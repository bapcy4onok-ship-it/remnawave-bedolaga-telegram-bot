"""Админ-роуты страницы «Чистая прибыль» в кабинете.

Доход берётся из реально прошедших платежей, расходы — из ``profit_expenses``
и выполненных заявок на вывод. Вся арифметика — в ``app/services/profit_service.py``.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.profit_expense import (
    create_expense,
    delete_expense,
    get_expense,
    get_expense_by_node,
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

    if days == 0:
        # 0 — «всё время», как в статистике продаж. Отличать от «не передано»
        # обязательно: иначе кнопка «Всё время» молча показывала бы текущий месяц.
        return datetime(2020, 1, 1, tzinfo=UTC), now

    if days is not None and days > 0:
        days = min(days, MAX_PERIOD_DAYS)
        start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now

    # Параметров нет — текущий месяц: «чистая прибыль» величина месячная.
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
    # Заполнены у строк вида «доля от прибыли»
    percent: float | None = None
    is_share: bool = False
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
    # Прибыль до долей — база, от которой считается процент каждой доли.
    profit_before_shares_kopeks: int = 0
    shares_total_kopeks: int = 0
    shares_percent_total: float = 0.0
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
    percent: float | None = None
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
    # Для kind='share' сумма не нужна — там процент; поэтому значение по умолчанию.
    amount: float = Field(default=0, ge=0)
    currency: str = 'RUB'
    percent: float | None = Field(default=None, ge=0, le=100)
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

    @model_validator(mode='after')
    def _check_dates(self) -> 'ExpenseWriteRequest':
        """Интервал счёта задаётся обоими концами и не вывернут наизнанку.

        Полузаполненный интервал раньше сохранялся молча и не попадал НИ В ОДИН
        отчёт: выборка требует оба конца, а подстановка даты срабатывала только
        когда пусты и она, и начало интервала. Расход был виден в редакторе,
        но в прибыль не входил, и признака ошибки не было.
        """
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError('period_start and period_end must be set together')

        # Сравнивать ТОЛЬКО после приведения к UTC: одна дата приходит из БД
        # (aware), вторая от клиента (naive ISO), и голое сравнение бросало
        # TypeError — pydantic заворачивает лишь ValueError, так что клиент
        # получал 500 вместо 422 на обычной правке одного поля пары.
        period_start, period_end = _as_utc(self.period_start), _as_utc(self.period_end)
        started_at, ended_at = _as_utc(self.started_at), _as_utc(self.ended_at)

        if period_start and period_end and period_start > period_end:
            raise ValueError('period_start must not be after period_end')
        if started_at and ended_at and started_at > ended_at:
            raise ValueError('started_at must not be after ended_at')
        return self

    @model_validator(mode='after')
    def _check_share(self) -> 'ExpenseWriteRequest':
        """Доля обязана нести процент, остальные виды — сумму, а не процент."""
        if self.kind == ProfitExpenseKind.SHARE.value:
            if not self.percent or self.percent <= 0:
                raise ValueError('percent is required for kind=share')
        else:
            self.percent = None
        return self

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


class ExpensePatchRequest(BaseModel):
    """PATCH: непереданные поля берутся из существующей строки, а не из дефолтов.

    Без этой схемы правка была разрушительной: ``_payload_to_fields`` всегда
    собирал ПОЛНЫЙ словарь колонок, и диалог серверов, шлющий только цену,
    обнулял «действует с/по» и заметки. Сервер, снятый в июне, после правки
    цены снова начислялся вечно.
    """

    model_config = ConfigDict(extra='forbid')

    category: str | None = None
    kind: str | None = None
    title: str | None = None
    group_name: str | None = None
    link: str | None = None
    amount: float | None = None
    currency: str | None = None
    percent: float | None = None
    spent_at: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    is_active: bool | None = None
    node_uuid: str | None = None
    color: str | None = None
    sort_order: int | None = None
    notes: str | None = None


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
        percent=expense.percent,
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


def _expense_to_write_fields(expense: ProfitExpense) -> dict[str, Any]:
    """Существующая строка в виде полей формы — основа для слияния при PATCH."""
    return {
        'category': expense.category,
        'kind': expense.kind,
        'title': expense.title,
        'group_name': expense.group_name,
        'link': expense.link,
        'amount': round(int(expense.amount_minor or 0) / 100, 2),
        'currency': expense.currency or 'RUB',
        'percent': expense.percent,
        # Полузаполненный интервал в БД (строки до валидатора) иначе делал бы
        # строку неправимой: любой PATCH падал бы в 422 из-за поля, которого
        # запрос даже не касался. Чиним на месте — одинокое начало интервала
        # становится датой траты.
        'spent_at': expense.spent_at or (expense.period_start if not expense.period_end else None),
        'period_start': expense.period_start if expense.period_end else None,
        'period_end': expense.period_end if expense.period_start else None,
        'started_at': expense.started_at,
        'ended_at': expense.ended_at,
        'is_active': bool(expense.is_active),
        'node_uuid': expense.node_uuid,
        'color': expense.color,
        'sort_order': int(expense.sort_order or 0),
        'notes': expense.notes,
    }


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
        'percent': payload.percent,
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

    if payload.kind == ProfitExpenseKind.ONCE.value and not payload.spent_at and not (payload.period_start and payload.period_end):
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


async def _apply_write(
    db: AsyncSession,
    expense: ProfitExpense,
    payload: 'ExpenseWriteRequest | ExpensePatchRequest',
) -> ProfitExpense:
    """Накладывает переданные поля на существующую строку.

    Слияние ДО валидации, а не после: правила вроде «у доли обязателен процент»
    должны проверяться на итоговом состоянии строки, а не на куске запроса.
    """
    try:
        merged = ExpenseWriteRequest(
            **{**_expense_to_write_fields(expense), **payload.model_dump(exclude_unset=True)}
        )
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error

    fields = await _payload_to_fields(merged, existing=expense)
    return await update_expense(db, expense, **fields)


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
    """Добавить строку расхода.

    Для строки, привязанной к ноде панели, это апсерт: у ноды может быть только
    одна стоимость (уникальный индекс), а быстрый ввод цен в диалоге серверов
    успевал отправить второй POST, пока не пришёл рефетч.
    """
    node_uuid = (payload.node_uuid or '').strip() or None
    if node_uuid:
        existing = await get_expense_by_node(db, node_uuid)
        if existing is not None:
            # Апсерт — это правка, а не замена: диалог серверов шлёт только цену,
            # и полный словарь дефолтов снёс бы «действует с/по» и заметки ровно
            # так же, как это делал PATCH до починки.
            return _to_response(await _apply_write(db, existing, payload))

    fields = await _payload_to_fields(payload)
    try:
        expense = await create_expense(db, created_by=admin.id, **fields)
    except IntegrityError:
        # Гонка двух одновременных запросов по одной ноде: проверка выше их
        # обоих пропустила, вставку выиграл первый. Второму нечего сообщать
        # об ошибке — он хотел ровно того же, что уже произошло.
        await db.rollback()
        existing = await get_expense_by_node(db, node_uuid) if node_uuid else None
        if existing is None:
            raise
        return _to_response(await _apply_write(db, existing, payload))

    return _to_response(expense)


@router.patch('/expenses/{expense_id}', response_model=ExpenseResponse)
async def update_profit_expense(
    expense_id: int,
    payload: ExpensePatchRequest,
    admin: User = Depends(require_permission('profit:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ExpenseResponse:
    """Изменить строку расхода. Непереданные поля остаются как были."""
    expense = await get_expense(db, expense_id)
    if expense is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Expense not found')

    return _to_response(await _apply_write(db, expense, payload))


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
    #
    # Только когда панели ВЕРЯТ: при её недоступности выдача пуста, и без этой
    # проверки все до единой строки аренды уезжали в раздел «вне панели» —
    # где их предлагают удалить, а правка обнуляет привязку к ноде.
    known_uuids = {node.uuid for node in nodes}
    orphaned = (
        [e for e in expenses if e.node_uuid and e.node_uuid not in known_uuids] if panel_available else []
    )

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
