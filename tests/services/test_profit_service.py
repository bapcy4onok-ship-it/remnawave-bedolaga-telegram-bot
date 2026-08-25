"""Тесты расчёта чистой прибыли: реальный доход, раскладка расходов по периоду.

БД поднимается в памяти точечным списком таблиц (см. tests/fixtures/sqlite_memory).
Курс валют подменяется фикстурой — сеть в тестах не дёргаем.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import (
    Donation,
    GuestPurchase,
    GuestPurchaseStatus,
    PaymentMethod,
    ProfitExpense,
    ProfitExpenseCategory,
    ProfitExpenseKind,
    Transaction,
    TransactionType,
    User,
    WithdrawalRequest,
    WithdrawalRequestStatus,
)
from app.services import profit_service
from app.services.exchange_rate_service import exchange_rate_service
from tests.fixtures.sqlite_memory import memory_session


TABLES = [
    User.__table__,
    Transaction.__table__,
    Donation.__table__,
    GuestPurchase.__table__,
    WithdrawalRequest.__table__,
    ProfitExpense.__table__,
]

PERIOD_START = datetime(2026, 8, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen_rates(monkeypatch):
    """Фиксированный курс: 1 $ = 100 ₽, 1 € = 110 ₽ — без похода в ЦБ."""
    monkeypatch.setattr(exchange_rate_service, '_rates', {'RUB': 1.0, 'USD': 100.0, 'EUR': 110.0})
    monkeypatch.setattr(exchange_rate_service, '_fetched_at', datetime.now(UTC))
    monkeypatch.setattr(exchange_rate_service, '_source', 'test')
    monkeypatch.setattr(exchange_rate_service, '_rate_date', '2026-08-24')


async def _add_user(session, user_id: int = 1, username: str = 'partner') -> User:
    user = User(id=user_id, telegram_id=1000 + user_id, username=username, first_name='Test')
    session.add(user)
    await session.commit()
    return user


def _transaction(**kwargs) -> Transaction:
    defaults = {
        'user_id': 1,
        'is_completed': True,
        'created_at': PERIOD_START + timedelta(days=5),
        'description': 'test',
    }
    return Transaction(**{**defaults, **kwargs})


# ── доход ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revenue_counts_only_real_gateway_money(monkeypatch):
    """Шлюзовые пополнения и прямые оплаты — в доход, оплата с баланса и ручное начисление — нет."""
    async with memory_session(monkeypatch, TABLES) as session:
        await _add_user(session)
        session.add_all(
            [
                _transaction(
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=100_000,
                    payment_method=PaymentMethod.YOOKASSA.value,
                ),
                _transaction(
                    type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                    amount_kopeks=-50_000,
                    payment_method=PaymentMethod.CRYPTOBOT.value,
                ),
                # Оплата с баланса: деньги уже посчитаны при пополнении.
                _transaction(
                    type=TransactionType.SUBSCRIPTION_PAYMENT.value,
                    amount_kopeks=-30_000,
                    payment_method=PaymentMethod.BALANCE.value,
                ),
                # Ручное начисление админом — не пришедшие деньги.
                _transaction(
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=70_000,
                    payment_method=PaymentMethod.MANUAL.value,
                ),
                # Незавершённый платёж не считается.
                _transaction(
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=999_000,
                    payment_method=PaymentMethod.YOOKASSA.value,
                    is_completed=False,
                ),
            ]
        )
        await session.commit()

        revenue = await profit_service.calculate_revenue(session, PERIOD_START, PERIOD_END)

    assert revenue['gateway_kopeks'] == 150_000
    assert revenue['total_kopeks'] == 150_000
    assert revenue['manual_topup_kopeks'] == 70_000


@pytest.mark.asyncio
async def test_revenue_adds_gifts_and_menu_donations_but_not_upsell(monkeypatch):
    """Подарки и донаты из меню — реальные деньги; надбавка к пополнению уже внутри депозита."""
    async with memory_session(monkeypatch, TABLES) as session:
        await _add_user(session)
        session.add_all(
            [
                _transaction(
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=100_000,
                    payment_method=PaymentMethod.YOOKASSA.value,
                ),
                GuestPurchase(
                    token='gift-1',
                    contact_type='telegram',
                    contact_value='42',
                    is_gift=True,
                    period_days=30,
                    amount_kopeks=40_000,
                    payment_method=PaymentMethod.YOOKASSA.value,
                    status=GuestPurchaseStatus.PAID.value,
                    paid_at=PERIOD_START + timedelta(days=3),
                ),
                # Подарок с баланса — уже посчитан в пополнении.
                GuestPurchase(
                    token='gift-2',
                    contact_type='telegram',
                    contact_value='43',
                    is_gift=True,
                    period_days=30,
                    amount_kopeks=25_000,
                    payment_method=PaymentMethod.BALANCE.value,
                    status=GuestPurchaseStatus.PAID.value,
                    paid_at=PERIOD_START + timedelta(days=3),
                ),
                Donation(
                    amount_kopeks=20_000,
                    source='menu',
                    payment_method=PaymentMethod.YOOKASSA.value,
                    created_at=PERIOD_START + timedelta(days=4),
                ),
                Donation(
                    amount_kopeks=1_000,
                    source='topup_upsell',
                    payment_method=PaymentMethod.YOOKASSA.value,
                    created_at=PERIOD_START + timedelta(days=4),
                ),
            ]
        )
        await session.commit()

        revenue = await profit_service.calculate_revenue(session, PERIOD_START, PERIOD_END)

    assert revenue['gifts_kopeks'] == 40_000
    assert revenue['donations_kopeks'] == 20_000
    assert revenue['total_kopeks'] == 160_000


# ── раскладка расходов ────────────────────────────────────────────────────────


def _monthly(**kwargs) -> ProfitExpense:
    defaults = {
        'category': ProfitExpenseCategory.SERVER.value,
        'kind': ProfitExpenseKind.MONTHLY.value,
        'title': 'node-1',
        'amount_minor': 300_000,  # 3000 ₽/мес
        'currency': 'RUB',
        'is_active': True,
    }
    return ProfitExpense(**{**defaults, **kwargs})


@pytest.mark.asyncio
async def test_monthly_expense_is_prorated_by_days(monkeypatch):
    """Месячная стоимость делится на среднюю длину месяца и умножается на дни периода."""
    async with memory_session(monkeypatch, TABLES) as session:
        session.add(_monthly())
        await session.commit()

        week_start = datetime(2026, 8, 10, tzinfo=UTC)
        week_end = week_start + timedelta(days=7)
        report = await profit_service.calculate_profit_report(session, week_start, week_end)

    expected = round(300_000 * 7 / profit_service.AVERAGE_MONTH_DAYS)
    assert report['expenses']['total_kopeks'] == expected
    assert report['expenses']['groups'][0]['category'] == ProfitExpenseCategory.SERVER.value


@pytest.mark.asyncio
async def test_monthly_expense_respects_active_window(monkeypatch):
    """Сервер, снятый в середине периода, считается только до даты снятия."""
    async with memory_session(monkeypatch, TABLES) as session:
        session.add(_monthly(ended_at=datetime(2026, 8, 11, tzinfo=UTC)))
        await session.commit()

        week_start = datetime(2026, 8, 8, tzinfo=UTC)
        week_end = week_start + timedelta(days=7)
        report = await profit_service.calculate_profit_report(session, week_start, week_end)

    expected = round(300_000 * 3 / profit_service.AVERAGE_MONTH_DAYS)
    assert report['expenses']['total_kopeks'] == expected


@pytest.mark.asyncio
async def test_currency_expense_uses_snapshot_then_live_rate(monkeypatch):
    """Разовый расход считается по зафиксированному курсу, регулярный — по живому."""
    async with memory_session(monkeypatch, TABLES) as session:
        session.add_all(
            [
                # 10 $ по зафиксированному курсу 80 ₽ = 800 ₽
                ProfitExpense(
                    category=ProfitExpenseCategory.AI.value,
                    kind=ProfitExpenseKind.ONCE.value,
                    title='ChatGPT',
                    amount_minor=1_000,
                    currency='USD',
                    fx_rate_snapshot=80.0,
                    spent_at=PERIOD_START + timedelta(days=2),
                ),
                # 10 € регулярно по живому курсу 110 ₽ = 1100 ₽/мес
                ProfitExpense(
                    category=ProfitExpenseCategory.SERVER.value,
                    kind=ProfitExpenseKind.MONTHLY.value,
                    title='Hetzner CX22',
                    group_name='Hetzner',
                    amount_minor=1_000,
                    currency='EUR',
                ),
            ]
        )
        await session.commit()

        month_end = PERIOD_START + timedelta(days=profit_service.AVERAGE_MONTH_DAYS)
        report = await profit_service.calculate_profit_report(session, PERIOD_START, month_end)

    by_category = {g['category']: g for g in report['expenses']['groups']}
    assert by_category[ProfitExpenseCategory.AI.value]['total_kopeks'] == 80_000
    assert by_category[ProfitExpenseCategory.SERVER.value]['total_kopeks'] == 110_000


@pytest.mark.asyncio
async def test_ads_entry_counts_only_inside_period(monkeypatch):
    """Рекламная запись попадает в отчёт целиком и только в свой период."""
    async with memory_session(monkeypatch, TABLES) as session:
        session.add_all(
            [
                ProfitExpense(
                    category=ProfitExpenseCategory.ADS.value,
                    kind=ProfitExpenseKind.ONCE.value,
                    title='@vpn_channel',
                    link='https://t.me/vpn_channel',
                    amount_minor=500_000,
                    spent_at=PERIOD_START + timedelta(days=1),
                ),
                ProfitExpense(
                    category=ProfitExpenseCategory.ADS.value,
                    kind=ProfitExpenseKind.ONCE.value,
                    title='@old_channel',
                    amount_minor=900_000,
                    spent_at=PERIOD_START - timedelta(days=40),
                ),
            ]
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(session, PERIOD_START, PERIOD_END)

    ads = next(g for g in report['expenses']['groups'] if g['category'] == ProfitExpenseCategory.ADS.value)
    assert ads['total_kopeks'] == 500_000
    assert [item['title'] for item in ads['items']] == ['@vpn_channel']


# ── CDN ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cdn_bill_is_split_by_overlap(monkeypatch):
    """Счёт за интервал делится пропорционально попавшим в период дням."""
    async with memory_session(monkeypatch, TABLES) as session:
        session.add(
            ProfitExpense(
                category=ProfitExpenseCategory.CDN.value,
                kind=ProfitExpenseKind.ONCE.value,
                title='Счёт за август',
                group_name='Selectel',
                amount_minor=310_000,  # 3100 ₽ за 31 день = 100 ₽/день
                period_start=datetime(2026, 8, 1, tzinfo=UTC),
                period_end=datetime(2026, 9, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(
            session,
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 11, tzinfo=UTC),
        )

    cdn = next(g for g in report['expenses']['groups'] if g['category'] == ProfitExpenseCategory.CDN.value)
    real = [item for item in cdn['items'] if not item['is_estimate']]
    assert real[0]['period_amount_kopeks'] == 100_000  # 10 дней × 100 ₽


@pytest.mark.asyncio
async def test_cdn_uncovered_days_use_last_month_average(monkeypatch):
    """Дни без счёта достраиваются по среднему за последний месяц — отдельной строкой-оценкой."""
    async with memory_session(monkeypatch, TABLES) as session:
        session.add(
            ProfitExpense(
                category=ProfitExpenseCategory.CDN.value,
                kind=ProfitExpenseKind.ONCE.value,
                title='Счёт за июль',
                group_name='Яндекс',
                amount_minor=310_000,  # 100 ₽/день
                period_start=datetime(2026, 7, 12, tzinfo=UTC),
                period_end=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )
        await session.commit()

        # Период — 12–22 августа: счёт кончился 12-го, 10 дней не покрыты.
        report = await profit_service.calculate_profit_report(
            session,
            datetime(2026, 8, 12, tzinfo=UTC),
            datetime(2026, 8, 22, tzinfo=UTC),
        )

    cdn = next(g for g in report['expenses']['groups'] if g['category'] == ProfitExpenseCategory.CDN.value)
    estimates = [item for item in cdn['items'] if item['is_estimate']]
    assert len(estimates) == 1
    assert estimates[0]['period_amount_kopeks'] == 100_000  # 10 дней × 100 ₽
    assert cdn['estimated_kopeks'] == 100_000
    assert report['expenses']['estimated_kopeks'] == 100_000


@pytest.mark.asyncio
async def test_cdn_fully_covered_period_has_no_estimate(monkeypatch):
    """Когда счёт покрывает весь период, прикидка не добавляется."""
    async with memory_session(monkeypatch, TABLES) as session:
        session.add(
            ProfitExpense(
                category=ProfitExpenseCategory.CDN.value,
                kind=ProfitExpenseKind.ONCE.value,
                title='Счёт за август',
                group_name='Selectel',
                amount_minor=310_000,
                period_start=datetime(2026, 8, 1, tzinfo=UTC),
                period_end=datetime(2026, 9, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(
            session,
            datetime(2026, 8, 5, tzinfo=UTC),
            datetime(2026, 8, 15, tzinfo=UTC),
        )

    assert report['expenses']['estimated_kopeks'] == 0


# ── партнёры ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_partner_payouts_come_from_completed_withdrawals(monkeypatch):
    """Выплаченные заявки на вывод попадают в расходы сами, невыплаченные — нет."""
    async with memory_session(monkeypatch, TABLES) as session:
        await _add_user(session, user_id=7, username='top_partner')
        session.add_all(
            [
                WithdrawalRequest(
                    user_id=7,
                    amount_kopeks=250_000,
                    status=WithdrawalRequestStatus.COMPLETED.value,
                    processed_at=PERIOD_START + timedelta(days=2),
                ),
                WithdrawalRequest(
                    user_id=7,
                    amount_kopeks=150_000,
                    status=WithdrawalRequestStatus.COMPLETED.value,
                    processed_at=PERIOD_START + timedelta(days=9),
                ),
                WithdrawalRequest(
                    user_id=7,
                    amount_kopeks=900_000,
                    status=WithdrawalRequestStatus.PENDING.value,
                    processed_at=PERIOD_START + timedelta(days=9),
                ),
            ]
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(session, PERIOD_START, PERIOD_END)

    partners = next(g for g in report['expenses']['groups'] if g['category'] == ProfitExpenseCategory.PARTNER.value)
    assert partners['total_kopeks'] == 400_000
    assert partners['items'][0]['title'] == '@top_partner'
    assert partners['items'][0]['payouts_count'] == 2
    assert partners['items'][0]['source'] == 'withdrawal'


# ── итог ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_net_profit_is_revenue_minus_expenses(monkeypatch):
    """Чистая прибыль и маржа считаются от реального дохода."""
    async with memory_session(monkeypatch, TABLES) as session:
        await _add_user(session)
        session.add_all(
            [
                _transaction(
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=1_000_000,  # 10 000 ₽
                    payment_method=PaymentMethod.YOOKASSA.value,
                ),
                ProfitExpense(
                    category=ProfitExpenseCategory.PERSONAL.value,
                    kind=ProfitExpenseKind.ONCE.value,
                    title='Никита',
                    amount_minor=400_000,  # 4000 ₽
                    spent_at=PERIOD_START + timedelta(days=6),
                ),
            ]
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(session, PERIOD_START, PERIOD_END)

    assert report['revenue']['total_kopeks'] == 1_000_000
    assert report['expenses']['total_kopeks'] == 400_000
    assert report['net_profit_kopeks'] == 600_000
    assert report['margin_percent'] == 60.0


@pytest.mark.asyncio
async def test_inactive_expense_is_ignored(monkeypatch):
    """Выключенная строка расходов в отчёт не попадает."""
    async with memory_session(monkeypatch, TABLES) as session:
        session.add(_monthly(is_active=False))
        await session.commit()

        report = await profit_service.calculate_profit_report(session, PERIOD_START, PERIOD_END)

    assert report['expenses']['total_kopeks'] == 0


@pytest.mark.asyncio
async def test_cdn_period_without_any_bill_still_estimated(monkeypatch):
    """Отчёт за текущий месяц, счёт за который ещё не выставлен, считается по среднему."""
    async with memory_session(monkeypatch, TABLES) as session:
        session.add(
            ProfitExpense(
                category=ProfitExpenseCategory.CDN.value,
                kind=ProfitExpenseKind.ONCE.value,
                title='Счёт за июль',
                group_name='Яндекс',
                amount_minor=310_000,  # 100 ₽/день
                period_start=datetime(2026, 7, 1, tzinfo=UTC),
                period_end=datetime(2026, 8, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        # 5–15 августа: ни один счёт в период не попадает.
        report = await profit_service.calculate_profit_report(
            session,
            datetime(2026, 8, 5, tzinfo=UTC),
            datetime(2026, 8, 15, tzinfo=UTC),
        )

    cdn = next(g for g in report['expenses']['groups'] if g['category'] == ProfitExpenseCategory.CDN.value)
    assert len(cdn['items']) == 1
    assert cdn['items'][0]['is_estimate'] is True
    assert cdn['items'][0]['period_amount_kopeks'] == 100_000  # 10 дней × 100 ₽


@pytest.mark.asyncio
async def test_cdn_without_recent_bills_is_not_guessed(monkeypatch):
    """Если усреднять нечего (последний счёт слишком старый) — оценки нет, а не ноль наугад."""
    async with memory_session(monkeypatch, TABLES) as session:
        session.add(
            ProfitExpense(
                category=ProfitExpenseCategory.CDN.value,
                kind=ProfitExpenseKind.ONCE.value,
                title='Счёт за январь',
                group_name='Яндекс',
                amount_minor=310_000,
                period_start=datetime(2026, 1, 1, tzinfo=UTC),
                period_end=datetime(2026, 2, 1, tzinfo=UTC),
            )
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(
            session,
            datetime(2026, 8, 5, tzinfo=UTC),
            datetime(2026, 8, 15, tzinfo=UTC),
        )

    assert report['expenses']['total_kopeks'] == 0
    assert report['expenses']['groups'] == []


@pytest.mark.asyncio
async def test_cdn_monthly_plan_is_not_estimated_on_top(monkeypatch):
    """Фиксированный месячный тариф CDN закрывает провайдера — прикидка сверху не нужна."""
    async with memory_session(monkeypatch, TABLES) as session:
        session.add(
            ProfitExpense(
                category=ProfitExpenseCategory.CDN.value,
                kind=ProfitExpenseKind.MONTHLY.value,
                title='Selectel CDN',
                group_name='Selectel',
                amount_minor=300_000,
            )
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(
            session,
            datetime(2026, 8, 5, tzinfo=UTC),
            datetime(2026, 8, 15, tzinfo=UTC),
        )

    assert report['expenses']['estimated_kopeks'] == 0
    cdn = next(g for g in report['expenses']['groups'] if g['category'] == ProfitExpenseCategory.CDN.value)
    assert all(not item['is_estimate'] for item in cdn['items'])


# ── доли от прибыли ───────────────────────────────────────────────────────────


def _share(percent: float, **kwargs) -> ProfitExpense:
    defaults = {
        'category': ProfitExpenseCategory.PERSONAL.value,
        'kind': ProfitExpenseKind.SHARE.value,
        'title': 'Никита',
        'percent': percent,
        'is_active': True,
    }
    return ProfitExpense(**{**defaults, **kwargs})


@pytest.mark.asyncio
async def test_share_is_percent_of_profit_after_other_expenses(monkeypatch):
    """20 % берутся не с дохода, а с того, что осталось после рекламы и серверов."""
    async with memory_session(monkeypatch, TABLES) as session:
        await _add_user(session)
        session.add_all(
            [
                _transaction(
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=1_000_000,  # доход 10 000 ₽
                    payment_method=PaymentMethod.YOOKASSA.value,
                ),
                ProfitExpense(
                    category=ProfitExpenseCategory.ADS.value,
                    kind=ProfitExpenseKind.ONCE.value,
                    title='@channel',
                    amount_minor=200_000,  # реклама 2000 ₽
                    spent_at=PERIOD_START + timedelta(days=2),
                ),
                _share(20),
            ]
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(session, PERIOD_START, PERIOD_END)

    # 10 000 − 2000 = 8000 ₽ до долей, доля 20 % = 1600 ₽, остаток 6400 ₽
    assert report['profit_before_shares_kopeks'] == 800_000
    assert report['shares_total_kopeks'] == 160_000
    assert report['net_profit_kopeks'] == 640_000
    assert report['expenses']['total_kopeks'] == 360_000

    personal = next(g for g in report['expenses']['groups'] if g['category'] == ProfitExpenseCategory.PERSONAL.value)
    assert personal['items'][0]['is_share'] is True
    assert personal['items'][0]['percent'] == 20.0


@pytest.mark.asyncio
async def test_fixed_pocket_entry_and_share_live_together(monkeypatch):
    """Фиксированная выплата остаётся расходом и уменьшает базу доли, а не спорит с ней."""
    async with memory_session(monkeypatch, TABLES) as session:
        await _add_user(session)
        session.add_all(
            [
                _transaction(
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=1_000_000,
                    payment_method=PaymentMethod.YOOKASSA.value,
                ),
                ProfitExpense(
                    category=ProfitExpenseCategory.PERSONAL.value,
                    kind=ProfitExpenseKind.ONCE.value,
                    title='Партнёру фиксом',
                    amount_minor=200_000,
                    spent_at=PERIOD_START + timedelta(days=3),
                ),
                _share(20),
            ]
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(session, PERIOD_START, PERIOD_END)

    assert report['profit_before_shares_kopeks'] == 800_000
    assert report['shares_total_kopeks'] == 160_000

    personal = next(g for g in report['expenses']['groups'] if g['category'] == ProfitExpenseCategory.PERSONAL.value)
    assert personal['total_kopeks'] == 360_000
    assert {item['title'] for item in personal['items']} == {'Партнёру фиксом', 'Никита'}


@pytest.mark.asyncio
async def test_shares_do_not_compound(monkeypatch):
    """Две доли считаются от одной базы, а не одна от остатка другой."""
    async with memory_session(monkeypatch, TABLES) as session:
        await _add_user(session)
        session.add_all(
            [
                _transaction(
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=1_000_000,
                    payment_method=PaymentMethod.YOOKASSA.value,
                ),
                _share(20, title='Никита'),
                _share(10, title='Партнёр'),
            ]
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(session, PERIOD_START, PERIOD_END)

    assert report['shares_total_kopeks'] == 300_000  # 200 000 + 100 000, а не 200 000 + 80 000
    assert report['shares_percent_total'] == 30.0
    assert report['net_profit_kopeks'] == 700_000


@pytest.mark.asyncio
async def test_share_is_zero_when_period_is_at_a_loss(monkeypatch):
    """Убыток на доли не делится: процент от минуса был бы «доплатой» себе."""
    async with memory_session(monkeypatch, TABLES) as session:
        await _add_user(session)
        session.add_all(
            [
                _transaction(
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=100_000,
                    payment_method=PaymentMethod.YOOKASSA.value,
                ),
                ProfitExpense(
                    category=ProfitExpenseCategory.ADS.value,
                    kind=ProfitExpenseKind.ONCE.value,
                    title='@channel',
                    amount_minor=500_000,
                    spent_at=PERIOD_START + timedelta(days=2),
                ),
                _share(20),
            ]
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(session, PERIOD_START, PERIOD_END)

    assert report['profit_before_shares_kopeks'] == -400_000
    assert report['shares_total_kopeks'] == 0
    assert report['net_profit_kopeks'] == -400_000


@pytest.mark.asyncio
async def test_share_started_midway_is_prorated(monkeypatch):
    """Доля, начавшая действовать в середине периода, берёт только свои дни."""
    async with memory_session(monkeypatch, TABLES) as session:
        await _add_user(session)
        session.add_all(
            [
                _transaction(
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=1_000_000,
                    payment_method=PaymentMethod.YOOKASSA.value,
                    created_at=datetime(2026, 8, 2, tzinfo=UTC),
                ),
                _share(20, started_at=datetime(2026, 8, 11, tzinfo=UTC)),
            ]
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(
            session,
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 21, tzinfo=UTC),
        )

    # 20 дней в периоде, доля действовала 10 → половина от 20 % с 10 000 ₽
    assert report['shares_total_kopeks'] == 100_000


@pytest.mark.asyncio
async def test_inactive_share_is_ignored(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as session:
        await _add_user(session)
        session.add_all(
            [
                _transaction(
                    type=TransactionType.DEPOSIT.value,
                    amount_kopeks=1_000_000,
                    payment_method=PaymentMethod.YOOKASSA.value,
                ),
                _share(20, is_active=False),
            ]
        )
        await session.commit()

        report = await profit_service.calculate_profit_report(session, PERIOD_START, PERIOD_END)

    assert report['shares_total_kopeks'] == 0
    assert report['net_profit_kopeks'] == 1_000_000
