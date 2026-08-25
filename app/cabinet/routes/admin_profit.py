"""Админ-роуты страницы «Чистая прибыль» в кабинете.

Доход берётся из реально прошедших платежей, расходы — из ``profit_expenses``
и выполненных заявок на вывод. Вся арифметика — в ``app/services/profit_service.py``.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.profit_expense import (
    create_expense,
    delete_expense,
    get_expense,
    list_expenses,
    update_expense,
)
from app.database.models import ProfitExpense, ProfitExpenseCategory, ProfitExpenseKind, User
from app.services.exchange_rate_service import SUPPORTED_CURRENCIES, exchange_rate_service
from app.services.profit_service import calculate_profit_report
from app.services.remnawave_service import RemnaWaveService

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/profit', tags=['Cabinet Admin Profit'])

MAX_PERIOD_DAYS = 730
CATEGORIES = tuple(c.value for c in ProfitExpenseCategory)
KINDS = tuple(k.value for k in ProfitExpenseKind)


# ============ Период ============


def _parse_period(days: int | None, start_date: str | None, end_date: str | None) -> tuple[datetime, datetime]:
    """Тот же разбор периода, что и в разделе продаж, — чтобы цифры сходились."""
    now = datetime.now(UTC)

    if start_date and end_date:
        try:
            start = datetime.fromisoformat(start_date)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid start_date format')
        try:
            end = datetime.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Invalid end_date format')

        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        if start > end:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='start_date must be before end_date')
        if (end - start).days > MAX_PERIOD_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Date range cannot exceed {MAX_PERIOD_DAYS} days',
            )
        return start, end.replace(hour=23, minute=59, second=59)

    if days is not None and days > 0:
        days = min(days, MAX_PERIOD_DAYS)
        start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now

    # По умолчанию — текущий месяц: «чистая прибыль» — величина месячная.
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now


# ============ Схемы ============


class ExpenseItem(BaseModel):
    """Строка расхода в отчёте (с уже посчитанной долей за период)."""

    id: int | None = None
    category: str
    kind: str
    title: str
    group_name: str | None = None
    link: str | None = None
    amount_minor: int = 0
    currency: str = 'RUB'
    full_amount_kopeks: int = 0
    period_amount_kopeks: int = 0
    spent_at: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    node_uuid: str | None = None
    color: str | None = None
    notes: str | None = None
    is_estimate: bool = False
    source: str = 'manual'
    # Только у автоматических строк
    payouts_count: int | None = None
    user_id: int | None = None
    uncovered_days: float | None = None
    daily_kopeks: int | None = None


class ExpenseSubgroup(BaseModel):
    name: str
    total_kopeks: int
    count: int
    color: str | None = None


class ExpenseGroup(BaseModel):
    category: str
    total_kopeks: int
    estimated_kopeks: int
    items: list[ExpenseItem]
    subgroups: list[ExpenseSubgroup]


class RevenueBreakdown(BaseModel):
    gateway_kopeks: int
    gifts_kopeks: int
    donations_kopeks: int
    total_kopeks: int
    # Ручные начисления админом — справочно, в total не входят.
    manual_topup_kopeks: int


class ExpensesBlock(BaseModel):
    total_kopeks: int
    estimated_kopeks: int
    groups: list[ExpenseGroup]


class PeriodInfo(BaseModel):
    start: datetime
    end: datetime
    days: float


class FxInfo(BaseModel):
    rates: dict[str, float]
    source: str
    date: str | None = None
    fetched_at: str | None = None


class ProfitReport(BaseModel):
    period: PeriodInfo
    revenue: RevenueBreakdown
    expenses: ExpensesBlock
    net_profit_kopeks: int
    margin_percent: float
    fx: FxInfo


class ExpenseResponse(BaseModel):
    """Сырая строка расхода (для редактора, без привязки к периоду)."""

    id: int
    category: str
    kind: str
    title: str
    group_name: str | None = None
    link: str | None = None
    amount_minor: int
    currency: str
    amount: float
    fx_rate_snapshot: float | None = None
    spent_at: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    is_active: bool
    node_uuid: str | None = None
    color: str | None = None
    sort_order: int
    notes: str | None = None
    created_at: datetime | None = None


class ExpenseWriteRequest(BaseModel):
    """Создание/правка строки. Сумма — в основных единицах валюты (₽/$/€)."""

    category: str
    kind: str = ProfitExpenseKind.ONCE.value
    title: str = Field(min_length=1, max_length=255)
    group_name: str | None = Field(default=None, max_length=128)
    link: str | None = Field(default=None, max_length=500)
    amount: float = Field(ge=0)
    currency: str = 'RUB'
    spent_at: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    is_active: bool = True
    node_uuid: str | None = Field(default=None, max_length=255)
    color: str | None = Field(default=None, max_length=16)
    sort_order: int = 0
    notes: str | None = None

    @field_validator('category')
    @classmethod
    def _check_category(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError(f'Unknown category: {value}')
        return value

    @field_validator('kind')
    @classmethod
    def _check_kind(cls, value: str) -> str:
        if value not in KINDS:
            raise ValueError(f'Unknown kind: {value}')
        return value

    @field_validator('currency')
    @classmethod
    def _check_currency(cls, value: str) -> str:
        code = (value or 'RUB').upper()
        if code not in SUPPORTED_CURRENCIES:
            raise ValueError(f'Unsupported currency: {value}')
        return code

    @field_validator('link')
    @classmethod
    def _check_link(cls, value: str | None) -> str | None:
        if not value:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith('@'):
            # Кабинет рендерит ссылку тегом <a>: голый @канал стал бы относительным
            # путём внутри кабинета. Разворачиваем в t.me сразу при сохранении.
            handle = stripped.lstrip('@')
            if not handle:
                return None
            return f'https://t.me/{handle}'
        if not stripped.startswith(('http://', 'https://', 'tg://')):
            raise ValueError('Link must start with http://, https://, tg:// or @')
        return stripped


class NodeCostItem(BaseModel):
    """Нода панели вместе с назначенной ей стоимостью (если назначена)."""

    uuid: str
    name: str
    address: str | None = None
    country_code: str | None = None
    is_connected: bool = False
    is_disabled: bool = False
    users_online: int = 0
    # Хостинг из панели Remnawave — подставляется как значение по умолчанию.
    provider_name: str | None = None
    expense: ExpenseResponse | None = None


class ServerCostsResponse(BaseModel):
    panel_available: bool
    nodes: list[NodeCostItem]
    # Строки категории «сервера», не привязанные к нодам панели (VPS бота, БД…)
    standalone: list[ExpenseResponse]
    monthly_total_kopeks: int


# ============ Хелперы ============


def _as_utc(value: datetime | None) -> datetime | None:
    """Наивная дата от клиента считается UTC — иначе Postgres истолкует её по своей таймзоне."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _as_end_of_day(value: datetime | None) -> datetime | None:
    """Верхняя граница, пришедшая датой без времени, означает «включительно по этот день».

    Клиент шлёт из календаря ``2026-08-31`` — человек имеет в виду весь 31-е,
    а полночь отрезала бы его целиком. Полночь ровно = дата без времени;
    указанное время уважаем как есть.
    """
    value = _as_utc(value)
    if value is None:
        return None
    if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
        return value.replace(hour=23, minute=59, second=59, microsecond=999999)
    return value


def _to_response(expense: ProfitExpense) -> ExpenseResponse:
    return ExpenseResponse(
        id=expense.id,
        category=expense.category,
        kind=expense.kind,
        title=expense.title,
        group_name=expense.group_name,
        link=expense.link,
        amount_minor=int(expense.amount_minor or 0),
        currency=expense.currency or 'RUB',
        amount=round(int(expense.amount_minor or 0) / 100, 2),
        fx_rate_snapshot=expense.fx_rate_snapshot,
        spent_at=expense.spent_at,
        period_start=expense.period_start,
        period_end=expense.period_end,
        started_at=expense.started_at,
        ended_at=expense.ended_at,
        is_active=bool(expense.is_active),
        node_uuid=expense.node_uuid,
        color=expense.color,
        sort_order=int(expense.sort_order or 0),
        notes=expense.notes,
        created_at=expense.created_at,
    )


async def _payload_to_fields(payload: ExpenseWriteRequest, *, existing: ProfitExpense | None = None) -> dict[str, Any]:
    """Схема запроса → поля модели (валюта в минорные единицы, курс — снимком)."""
    amount_minor = int(round(payload.amount * 100))

    fields: dict[str, Any] = {
        'category': payload.category,
        'kind': payload.kind,
        'title': payload.title.strip(),
        'group_name': (payload.group_name or '').strip() or None,
        'link': payload.link,
        'amount_minor': amount_minor,
        'currency': payload.currency,
        'spent_at': _as_utc(payload.spent_at),
        'period_start': _as_utc(payload.period_start),
        'period_end': _as_end_of_day(payload.period_end),
        'started_at': _as_utc(payload.started_at),
        'ended_at': _as_end_of_day(payload.ended_at),
        'is_active': payload.is_active,
        'node_uuid': (payload.node_uuid or '').strip() or None,
        'color': (payload.color or '').strip() or None,
        'sort_order': payload.sort_order,
        'notes': (payload.notes or '').strip() or None,
    }

    if payload.kind == ProfitExpenseKind.ONCE.value and not payload.spent_at and not payload.period_start:
        # Без даты разовый расход не попал бы ни в один период — молча терять
        # запись хуже, чем поставить сегодняшний день.
        fields['spent_at'] = datetime.now(UTC)

    if payload.currency == 'RUB':
        fields['fx_rate_snapshot'] = None
    elif payload.kind == ProfitExpenseKind.ONCE.value:
        # Оплаченный счёт стоил столько, сколько стоил тогда: фиксируем курс,
        # чтобы отчёт за прошлый месяц не менялся вслед за сегодняшним ЦБ.
        # Пересчитываем только когда сумма или валюта действительно изменились.
        keep_rate = (
            existing is not None
            and existing.fx_rate_snapshot
            and existing.currency == payload.currency
            and existing.amount_minor == amount_minor
        )
        fields['fx_rate_snapshot'] = (
            existing.fx_rate_snapshot if keep_rate else await exchange_rate_service.get_rate(payload.currency)
        )
    else:
        # Регулярный расход считается по живому курсу — снимок только мешал бы.
        fields['fx_rate_snapshot'] = None

    return fields


# ============ Отчёт ============


@router.get('/summary', response_model=ProfitReport)
async def get_profit_summary(
    days: int | None = Query(default=None, description='Период в днях (7, 30, 90); пусто — текущий месяц'),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    admin: User = Depends(require_permission('profit:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ProfitReport:
    """Чистая прибыль за период: доход минус все расходы."""
    period_start, period_end = _parse_period(days, start_date, end_date)
    try:
        report = await calculate_profit_report(db, period_start, period_end)
        return ProfitReport(**report)
    except Exception as error:
        logger.error('Не удалось построить отчёт по прибыли', error=error, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to build profit report',
        )


@router.get('/rates')
async def get_exchange_rates(
    admin: User = Depends(require_permission('profit:read')),
) -> FxInfo:
    """Актуальные курсы ЦБ, по которым пересчитываются валютные расходы."""
    return FxInfo(**await exchange_rate_service.get_rates())


# ============ Строки расходов ============


@router.get('/expenses', response_model=list[ExpenseResponse])
async def list_profit_expenses(
    category: str | None = Query(default=None),
    include_inactive: bool = Query(default=True),
    admin: User = Depends(require_permission('profit:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> list[ExpenseResponse]:
    """Все заведённые строки расходов (для редактора)."""
    if category and category not in CATEGORIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'Unknown category: {category}')

    expenses = await list_expenses(
        db,
        categories=[category] if category else None,
        include_inactive=include_inactive,
    )
    return [_to_response(expense) for expense in expenses]


@router.post('/expenses', response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
async def create_profit_expense(
    payload: ExpenseWriteRequest,
    admin: User = Depends(require_permission('profit:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ExpenseResponse:
    """Добавить строку расхода."""
    fields = await _payload_to_fields(payload)
    expense = await create_expense(db, created_by=admin.id, **fields)
    return _to_response(expense)


@router.patch('/expenses/{expense_id}', response_model=ExpenseResponse)
async def update_profit_expense(
    expense_id: int,
    payload: ExpenseWriteRequest,
    admin: User = Depends(require_permission('profit:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ExpenseResponse:
    """Изменить строку расхода."""
    expense = await get_expense(db, expense_id)
    if expense is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Expense not found')

    fields = await _payload_to_fields(payload, existing=expense)
    updated = await update_expense(db, expense, **fields)
    return _to_response(updated)


@router.delete('/expenses/{expense_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_profit_expense(
    expense_id: int,
    admin: User = Depends(require_permission('profit:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Удалить строку расхода."""
    if not await delete_expense(db, expense_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Expense not found')


# ============ Сервера ============


@router.get('/servers', response_model=ServerCostsResponse)
async def get_server_costs(
    admin: User = Depends(require_permission('profit:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ServerCostsResponse:
    """Список серверов панели со стоимостью аренды.

    Ноды тянутся из Remnawave, чтобы список не приходилось вести руками;
    хостинг подставляется из ``provider_name`` панели, пока админ не задал свой.
    Недоступность панели не ломает страницу — отдаём заведённые строки.
    """
    expenses = await list_expenses(db, categories=[ProfitExpenseCategory.SERVER.value])
    by_node = {e.node_uuid: e for e in expenses if e.node_uuid}
    standalone = [e for e in expenses if not e.node_uuid]

    nodes: list[NodeCostItem] = []
    panel_available = True
    try:
        raw_nodes = await RemnaWaveService().get_all_nodes()
    except Exception as error:
        logger.warning('Панель Remnawave недоступна, список нод пуст', error=str(error))
        raw_nodes = []
        panel_available = False
    else:
        panel_available = bool(raw_nodes)

    for node in raw_nodes:
        uuid = node.get('uuid') or ''
        expense = by_node.get(uuid)
        nodes.append(
            NodeCostItem(
                uuid=uuid,
                name=node.get('name') or 'Unknown',
                address=node.get('address'),
                country_code=node.get('country_code'),
                is_connected=bool(node.get('is_connected')),
                is_disabled=bool(node.get('is_disabled')),
                users_online=int(node.get('users_online') or 0),
                provider_name=node.get('provider_name'),
                expense=_to_response(expense) if expense else None,
            )
        )

    # Строки, привязанные к нодам, которых панель уже не отдаёт (нода удалена),
    # иначе они пропали бы из редактора, продолжая попадать в расходы.
    known_uuids = {node.uuid for node in nodes}
    orphaned = [e for e in expenses if e.node_uuid and e.node_uuid not in known_uuids]

    monthly_total = 0
    for expense in expenses:
        if expense.is_active and expense.kind == ProfitExpenseKind.MONTHLY.value:
            monthly_total += await exchange_rate_service.to_rub_kopeks(
                int(expense.amount_minor or 0),
                expense.currency or 'RUB',
                rate=expense.fx_rate_snapshot,
            )

    return ServerCostsResponse(
        panel_available=panel_available,
        nodes=nodes,
        standalone=[_to_response(e) for e in standalone + orphaned],
        monthly_total_kopeks=monthly_total,
    )
