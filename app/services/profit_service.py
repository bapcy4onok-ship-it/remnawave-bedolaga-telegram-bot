"""Расчёт чистой прибыли: реальный доход минус расходы.

Доход берётся ТОЛЬКО из фактически прошедших через шлюзы платежей — те же
предикаты, что и у карточки «Доход» в разделе продаж, чтобы цифры сходились:
пополнения баланса + прямые оплаты подписок + подарки, оплаченные шлюзом,
+ пожертвования. Начисления админом вручную (``payment_method='manual'``) в
доход НЕ идут — это не пришедшие деньги; они отдаются отдельной строкой
справочно.

Расходы приходят из ``profit_expenses`` (заводит админ) и из заявок на вывод
реферального баланса (``withdrawal_requests`` со статусом «выполнена») —
последние тянутся автоматически, руками их дублировать не надо.

Регулярные (месячные) расходы раскладываются по дням: доля = месячная
стоимость × (дней в периоде / 30.4375). Поэтому «за 7 дней» и «за месяц»
сравнимы между собой.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.profit_expense import list_expenses_for_period
from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import (
    Donation,
    GuestPurchase,
    PaymentMethod,
    ProfitExpense,
    ProfitExpenseCategory,
    ProfitExpenseKind,
    Transaction,
    TransactionType,
    WithdrawalRequest,
    WithdrawalRequestStatus,
)
from app.services.exchange_rate_service import exchange_rate_service


logger = structlog.get_logger(__name__)

# Средняя длина месяца (365.25 / 12) — по ней месячная стоимость превращается
# в дневную. Календарные месяцы разной длины дали бы скачки «прибыль в феврале
# выше просто потому, что февраль короче».
AVERAGE_MONTH_DAYS = 365.25 / 12

# Надбавка к пополнению («добавить 10 ₽ на поддержку») списывается С БАЛАНСА,
# который шлюз уже зачислил, — она сидит внутри суммы пополнения. Считать её
# ещё раз в доходе значило бы удвоить эти деньги.
UPSELL_DONATION_SOURCE = 'topup_upsell'

# Окно, по которому усредняются счета CDN, когда период отчёта ими не покрыт.
CDN_AVERAGE_WINDOW_DAYS = 30

# Порядок категорий в отчёте — он же порядок блоков на странице.
CATEGORY_ORDER = (
    ProfitExpenseCategory.SERVER.value,
    ProfitExpenseCategory.ADS.value,
    ProfitExpenseCategory.CDN.value,
    ProfitExpenseCategory.PERSONAL.value,
    ProfitExpenseCategory.PARTNER.value,
    ProfitExpenseCategory.STAFF.value,
    ProfitExpenseCategory.AI.value,
    ProfitExpenseCategory.OTHER.value,
)


# ── время ─────────────────────────────────────────────────────────────────────


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _overlap_days(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime | None,
    b_end: datetime | None,
) -> float:
    """Пересечение двух интервалов в днях (0, если не пересекаются)."""
    start = max(a_start, _aware(b_start) or a_start)
    end = min(a_end, _aware(b_end) or a_end)
    if end <= start:
        return 0.0
    return (end - start).total_seconds() / 86400


def _period_days(period_start: datetime, period_end: datetime) -> float:
    return max((period_end - period_start).total_seconds() / 86400, 0.0)


def _merge_intervals(intervals: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Схлопывает пересекающиеся интервалы — чтобы дни не считались дважды."""
    ordered = sorted(intervals, key=lambda pair: pair[0])
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


# ── доход ─────────────────────────────────────────────────────────────────────


async def _gateway_revenue_kopeks(db: AsyncSession, period_start: datetime, period_end: datetime) -> int:
    """Пополнения баланса + прямые оплаты подписок, прошедшие через шлюзы."""
    result = await db.execute(
        select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
            and_(
                Transaction.type.in_([TransactionType.DEPOSIT.value, TransactionType.SUBSCRIPTION_PAYMENT.value]),
                Transaction.is_completed == True,
                Transaction.payment_method.in_(REAL_PAYMENT_METHODS),
                Transaction.created_at >= period_start,
                Transaction.created_at <= period_end,
            )
        )
    )
    return int(result.scalar() or 0)


async def _gift_revenue_kopeks(db: AsyncSession, period_start: datetime, period_end: datetime) -> int:
    """Подарки, оплаченные шлюзом.

    Транзакции они не создают (получатель «не платил»), поэтому без этой суммы
    реально пришедшие деньги терялись бы. Подарки с баланса сюда не входят —
    они уже учтены в пополнении, которым баланс был наполнен.
    """
    result = await db.execute(
        select(func.coalesce(func.sum(GuestPurchase.amount_kopeks), 0)).where(
            and_(
                GuestPurchase.is_gift.is_(True),
                GuestPurchase.payment_method.in_(REAL_PAYMENT_METHODS),
                GuestPurchase.paid_at >= period_start,
                GuestPurchase.paid_at <= period_end,
            )
        )
    )
    return int(result.scalar() or 0)


async def _donation_revenue_kopeks(db: AsyncSession, period_start: datetime, period_end: datetime) -> int:
    """Пожертвования, пришедшие отдельным платежом (без надбавок к пополнению)."""
    result = await db.execute(
        select(func.coalesce(func.sum(Donation.amount_kopeks), 0)).where(
            and_(
                func.coalesce(Donation.source, 'menu') != UPSELL_DONATION_SOURCE,
                Donation.created_at >= period_start,
                Donation.created_at <= period_end,
            )
        )
    )
    return int(result.scalar() or 0)


async def _manual_topup_kopeks(db: AsyncSession, period_start: datetime, period_end: datetime) -> int:
    """Ручные начисления админом — справочно, в доход не входят."""
    result = await db.execute(
        select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0)).where(
            and_(
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.is_completed == True,
                Transaction.payment_method == PaymentMethod.MANUAL.value,
                Transaction.created_at >= period_start,
                Transaction.created_at <= period_end,
            )
        )
    )
    return int(result.scalar() or 0)


async def calculate_revenue(db: AsyncSession, period_start: datetime, period_end: datetime) -> dict[str, int]:
    """Реальный доход за период с разбивкой по источникам."""
    gateway = await _gateway_revenue_kopeks(db, period_start, period_end)
    gifts = await _gift_revenue_kopeks(db, period_start, period_end)
    donations = await _donation_revenue_kopeks(db, period_start, period_end)
    manual = await _manual_topup_kopeks(db, period_start, period_end)

    return {
        'gateway_kopeks': gateway,
        'gifts_kopeks': gifts,
        'donations_kopeks': donations,
        'total_kopeks': gateway + gifts + donations,
        'manual_topup_kopeks': manual,
    }


# ── расходы ───────────────────────────────────────────────────────────────────


async def _expense_full_kopeks(expense: ProfitExpense) -> int:
    """Вся сумма записи в копейках (с пересчётом валюты)."""
    return await exchange_rate_service.to_rub_kopeks(
        int(expense.amount_minor or 0),
        expense.currency or 'RUB',
        rate=expense.fx_rate_snapshot,
    )


def _allocate(expense: ProfitExpense, full_kopeks: int, period_start: datetime, period_end: datetime) -> int:
    """Сколько из записи приходится на выбранный период."""
    if expense.kind == ProfitExpenseKind.MONTHLY.value:
        days = _overlap_days(period_start, period_end, expense.started_at, expense.ended_at)
        return int(round(full_kopeks * days / AVERAGE_MONTH_DAYS))

    start = _aware(expense.period_start)
    end = _aware(expense.period_end)
    if start and end and end > start:
        # Счёт за интервал (типично для CDN): делим пропорционально пересечению.
        bill_days = (end - start).total_seconds() / 86400
        overlap = _overlap_days(period_start, period_end, start, end)
        return int(round(full_kopeks * overlap / bill_days))

    spent_at = _aware(expense.spent_at) or _aware(expense.created_at)
    if spent_at and period_start <= spent_at <= period_end:
        return full_kopeks
    return 0


async def _expense_to_item(
    expense: ProfitExpense,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, Any]:
    full = await _expense_full_kopeks(expense)
    return {
        'id': expense.id,
        'category': expense.category,
        'kind': expense.kind,
        'title': expense.title,
        'group_name': expense.group_name,
        'link': expense.link,
        'amount_minor': int(expense.amount_minor or 0),
        'currency': expense.currency or 'RUB',
        'full_amount_kopeks': full,
        'period_amount_kopeks': _allocate(expense, full, period_start, period_end),
        'spent_at': _aware(expense.spent_at),
        'period_start': _aware(expense.period_start),
        'period_end': _aware(expense.period_end),
        'started_at': _aware(expense.started_at),
        'ended_at': _aware(expense.ended_at),
        'node_uuid': expense.node_uuid,
        'color': expense.color,
        'notes': expense.notes,
        'percent': expense.percent,
        'is_share': expense.kind == ProfitExpenseKind.SHARE.value,
        'is_estimate': False,
        'source': 'manual',
    }


async def _partner_payout_items(
    db: AsyncSession,
    period_start: datetime,
    period_end: datetime,
) -> list[dict[str, Any]]:
    """Выполненные заявки на вывод реферального баланса — по одной строке на партнёра."""
    from app.database.models import User

    result = await db.execute(
        select(
            WithdrawalRequest.user_id,
            User.telegram_id,
            User.username,
            User.first_name,
            func.count(WithdrawalRequest.id).label('payouts'),
            func.coalesce(func.sum(WithdrawalRequest.amount_kopeks), 0).label('amount'),
            func.max(func.coalesce(WithdrawalRequest.processed_at, WithdrawalRequest.updated_at)).label('last_at'),
        )
        .join(User, User.id == WithdrawalRequest.user_id)
        .where(
            and_(
                WithdrawalRequest.status == WithdrawalRequestStatus.COMPLETED.value,
                func.coalesce(WithdrawalRequest.processed_at, WithdrawalRequest.updated_at) >= period_start,
                func.coalesce(WithdrawalRequest.processed_at, WithdrawalRequest.updated_at) <= period_end,
            )
        )
        .group_by(WithdrawalRequest.user_id, User.telegram_id, User.username, User.first_name)
        .order_by(func.coalesce(func.sum(WithdrawalRequest.amount_kopeks), 0).desc())
    )

    items: list[dict[str, Any]] = []
    for row in result.all():
        amount = int(row.amount or 0)
        display = f'@{row.username}' if row.username else (row.first_name or f'ID {row.telegram_id or row.user_id}')
        items.append(
            {
                'id': None,
                'category': ProfitExpenseCategory.PARTNER.value,
                'kind': ProfitExpenseKind.ONCE.value,
                'title': display,
                'group_name': None,
                'link': None,
                'amount_minor': amount,
                'currency': 'RUB',
                'full_amount_kopeks': amount,
                'period_amount_kopeks': amount,
                'spent_at': _aware(row.last_at),
                'period_start': None,
                'period_end': None,
                'started_at': None,
                'ended_at': None,
                'node_uuid': None,
                'color': None,
                'notes': None,
                'payouts_count': int(row.payouts or 0),
                'user_id': row.user_id,
                'percent': None,
                'is_share': False,
                'is_estimate': False,
                # Не редактируется в кабинете: источник — заявки на вывод.
                'source': 'withdrawal',
            }
        )
    return items


async def _cdn_estimate_items(
    db: AsyncSession,
    cdn_expenses: list[ProfitExpense],
    period_start: datetime,
    period_end: datetime,
) -> list[dict[str, Any]]:
    """Достраивает расходы CDN на дни периода, не покрытые счетами.

    Правило из задачи: если период отчёта не совпадает со счетами, недостающие
    дни считаются по среднему за последний месяц. Оценка идёт отдельной строкой
    с ``is_estimate=True`` — реальные счета и прикидка не должны смешиваться.

    Провайдеры берутся не только из счетов, попавших в период: типичный случай —
    отчёт за текущий месяц, счёт за который ещё не выставлен. Тогда в периоде
    счетов нет вообще, и считать по нулю было бы хуже, чем по среднему.
    """
    total_days = _period_days(period_start, period_end)
    if total_days <= 0:
        return []

    def provider_of(expense: ProfitExpense) -> str:
        return (expense.group_name or expense.title or 'CDN').strip()

    # Счета за последний месяц перед концом периода — база для среднедневной.
    average_window_start = period_end - timedelta(days=CDN_AVERAGE_WINDOW_DAYS)
    recent = await list_expenses_for_period(
        db,
        average_window_start,
        period_end,
        categories=[ProfitExpenseCategory.CDN.value],
    )
    if not recent and not cdn_expenses:
        return []

    recent_by_provider: dict[str, list[ProfitExpense]] = {}
    for expense in recent:
        recent_by_provider.setdefault(provider_of(expense), []).append(expense)

    in_period_by_provider: dict[str, list[ProfitExpense]] = {}
    for expense in cdn_expenses:
        in_period_by_provider.setdefault(provider_of(expense), []).append(expense)

    items: list[dict[str, Any]] = []
    for provider in sorted({*in_period_by_provider, *recent_by_provider}):
        in_period = in_period_by_provider.get(provider, [])

        # Регулярная строка закрывает провайдера целиком — достраивать нечего.
        if any(e.kind == ProfitExpenseKind.MONTHLY.value for e in in_period):
            continue

        covered: list[tuple[datetime, datetime]] = []
        for expense in in_period:
            start = _aware(expense.period_start)
            end = _aware(expense.period_end)
            if start and end and end > start:
                clipped_start = max(start, period_start)
                clipped_end = min(end, period_end)
                if clipped_end > clipped_start:
                    covered.append((clipped_start, clipped_end))

        covered_days = sum((end - start).total_seconds() / 86400 for start, end in _merge_intervals(covered))
        uncovered_days = total_days - covered_days
        if uncovered_days <= 0.5:  # меньше половины суток — округление, не дыра
            continue

        daily_kopeks = await _average_daily_kopeks(
            recent_by_provider.get(provider, []),
            average_window_start,
            period_end,
        )
        if daily_kopeks <= 0:
            continue

        estimated = int(round(daily_kopeks * uncovered_days))
        items.append(
            {
                'id': None,
                'category': ProfitExpenseCategory.CDN.value,
                'kind': ProfitExpenseKind.ONCE.value,
                'title': provider,
                'group_name': provider,
                'link': None,
                'amount_minor': 0,
                'currency': 'RUB',
                'full_amount_kopeks': estimated,
                'period_amount_kopeks': estimated,
                'spent_at': None,
                'period_start': None,
                'period_end': None,
                'started_at': None,
                'ended_at': None,
                'node_uuid': None,
                'color': None,
                'notes': None,
                'uncovered_days': round(uncovered_days, 1),
                'daily_kopeks': int(round(daily_kopeks)),
                'percent': None,
                'is_share': False,
                'is_estimate': True,
                'source': 'estimate',
            }
        )

    return items


async def _average_daily_kopeks(
    expenses: list[ProfitExpense],
    window_start: datetime,
    window_end: datetime,
) -> float:
    """Средний расход в день по счетам, попавшим в окно усреднения.

    Счёт за интервал даёт свою дневную ставку с весом «сколько его дней попало
    в окно»; счёт без интервала — это платёж, его сумма просто размазывается
    по всему окну.
    """
    weighted_sum = 0.0
    weight = 0.0
    flat_kopeks = 0
    window_days = max((window_end - window_start).total_seconds() / 86400, 1.0)

    for expense in expenses:
        full = await _expense_full_kopeks(expense)
        start = _aware(expense.period_start)
        end = _aware(expense.period_end)
        if start and end and end > start:
            bill_days = (end - start).total_seconds() / 86400
            overlap = _overlap_days(window_start, window_end, start, end)
            if overlap > 0 and bill_days > 0:
                weighted_sum += (full / bill_days) * overlap
                weight += overlap
        else:
            spent_at = _aware(expense.spent_at) or _aware(expense.created_at)
            if spent_at and window_start <= spent_at <= window_end:
                flat_kopeks += full

    daily = (weighted_sum / weight) if weight > 0 else 0.0
    return daily + flat_kopeks / window_days


# ── отчёт ─────────────────────────────────────────────────────────────────────


async def _share_items(
    shares: list[ProfitExpense],
    base_kopeks: int,
    period_start: datetime,
    period_end: datetime,
) -> list[dict[str, Any]]:
    """Доли от прибыли: процент от того, что осталось после остальных расходов.

    База не может быть отрицательной: делить убыток на доли бессмысленно —
    при минусе доля равна нулю, а не «минус 20 %».

    Если доля действовала не весь период (появилась 15-го), она уменьшается
    пропорционально своим дням: точнее было бы считать прибыль за эти дни
    отдельно, но это потребовало бы пересчёта всего отчёта на каждую долю.
    """
    base = max(base_kopeks, 0)
    period_days = _period_days(period_start, period_end)
    items: list[dict[str, Any]] = []

    for share in shares:
        percent = float(share.percent or 0)
        if percent <= 0:
            continue

        active_days = _overlap_days(period_start, period_end, share.started_at, share.ended_at)
        fraction = (active_days / period_days) if period_days > 0 else 0.0
        amount = int(round(base * percent / 100 * fraction))

        items.append(
            {
                'id': share.id,
                'category': share.category,
                'kind': ProfitExpenseKind.SHARE.value,
                'title': share.title,
                'group_name': share.group_name,
                'link': share.link,
                'amount_minor': 0,
                'currency': 'RUB',
                'full_amount_kopeks': amount,
                'period_amount_kopeks': amount,
                'spent_at': None,
                'period_start': None,
                'period_end': None,
                'started_at': _aware(share.started_at),
                'ended_at': _aware(share.ended_at),
                'node_uuid': None,
                'color': share.color,
                'notes': share.notes,
                'percent': percent,
                'is_share': True,
                'is_estimate': False,
                'source': 'manual',
            }
        )

    return items


async def calculate_profit_report(
    db: AsyncSession,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, Any]:
    """Полный отчёт: доход, расходы по категориям, доли от прибыли, чистая прибыль."""
    revenue = await calculate_revenue(db, period_start, period_end)

    expenses = await list_expenses_for_period(db, period_start, period_end)
    shares = [e for e in expenses if e.kind == ProfitExpenseKind.SHARE.value]
    regular = [e for e in expenses if e.kind != ProfitExpenseKind.SHARE.value]

    items_by_category: dict[str, list[dict[str, Any]]] = {}
    for expense in regular:
        item = await _expense_to_item(expense, period_start, period_end)
        if item['period_amount_kopeks'] == 0 and expense.kind == ProfitExpenseKind.MONTHLY.value:
            continue
        items_by_category.setdefault(expense.category, []).append(item)

    cdn_estimates = await _cdn_estimate_items(
        db,
        [e for e in regular if e.category == ProfitExpenseCategory.CDN.value],
        period_start,
        period_end,
    )
    if cdn_estimates:
        items_by_category.setdefault(ProfitExpenseCategory.CDN.value, []).extend(cdn_estimates)

    partner_items = await _partner_payout_items(db, period_start, period_end)
    if partner_items:
        items_by_category.setdefault(ProfitExpenseCategory.PARTNER.value, []).extend(partner_items)

    # Доли считаются вторым проходом: их база — прибыль после ВСЕХ остальных
    # расходов, поэтому раньше этого места её просто не существует.
    expenses_before_shares = sum(
        item['period_amount_kopeks'] for items in items_by_category.values() for item in items
    )
    profit_before_shares = revenue['total_kopeks'] - expenses_before_shares

    share_items = await _share_items(shares, profit_before_shares, period_start, period_end)
    for item in share_items:
        items_by_category.setdefault(item['category'], []).append(item)
    shares_total = sum(item['period_amount_kopeks'] for item in share_items)

    groups: list[dict[str, Any]] = []
    total_expenses = 0
    total_estimated = 0

    for category in CATEGORY_ORDER:
        items = items_by_category.get(category, [])
        if not items:
            continue
        items.sort(key=lambda i: i['period_amount_kopeks'], reverse=True)
        category_total = sum(i['period_amount_kopeks'] for i in items)
        estimated = sum(i['period_amount_kopeks'] for i in items if i['is_estimate'])
        total_expenses += category_total
        total_estimated += estimated

        groups.append(
            {
                'category': category,
                'total_kopeks': category_total,
                'estimated_kopeks': estimated,
                'items': items,
                'subgroups': _subgroups(items),
            }
        )

    net_profit = revenue['total_kopeks'] - total_expenses
    margin = round(net_profit / revenue['total_kopeks'] * 100, 1) if revenue['total_kopeks'] > 0 else 0.0

    fx = await exchange_rate_service.get_rates()

    return {
        'period': {
            'start': period_start,
            'end': period_end,
            'days': round(_period_days(period_start, period_end), 2),
        },
        'revenue': revenue,
        'expenses': {
            'total_kopeks': total_expenses,
            'estimated_kopeks': total_estimated,
            'groups': groups,
        },
        # База, от которой считаются доли, и сколько по ним ушло.
        'profit_before_shares_kopeks': profit_before_shares,
        'shares_total_kopeks': shares_total,
        'shares_percent_total': round(sum(float(s.percent or 0) for s in shares), 2),
        'net_profit_kopeks': net_profit,
        'margin_percent': margin,
        'fx': fx,
    }


def _subgroups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Разбивка категории по ``group_name`` — хостинги у серверов, провайдеры у CDN."""
    totals: dict[str, dict[str, Any]] = {}
    for item in items:
        name = (item.get('group_name') or '').strip()
        if not name:
            continue
        bucket = totals.setdefault(name, {'name': name, 'total_kopeks': 0, 'count': 0, 'color': item.get('color')})
        bucket['total_kopeks'] += item['period_amount_kopeks']
        bucket['count'] += 1
        if not bucket['color'] and item.get('color'):
            bucket['color'] = item['color']

    return sorted(totals.values(), key=lambda g: g['total_kopeks'], reverse=True)
