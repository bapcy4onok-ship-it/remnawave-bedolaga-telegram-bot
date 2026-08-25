"""Тесты хелперов и валидации админ-роутов «Чистая прибыль»."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.cabinet.routes.admin_profit import ExpenseWriteRequest, _parse_period, _payload_to_fields
from app.database.models import ProfitExpense, ProfitExpenseCategory, ProfitExpenseKind
from app.services.exchange_rate_service import exchange_rate_service


@pytest.fixture(autouse=True)
def frozen_rates(monkeypatch):
    monkeypatch.setattr(exchange_rate_service, '_rates', {'RUB': 1.0, 'USD': 100.0, 'EUR': 110.0})
    monkeypatch.setattr(exchange_rate_service, '_fetched_at', datetime.now(UTC))


def test_period_defaults_to_current_month():
    """Без параметров отчёт строится за текущий месяц — прибыль величина месячная."""
    start, end = _parse_period(None, None, None)
    now = datetime.now(UTC)
    assert start.day == 1 and start.hour == 0
    assert start.month == now.month
    assert end <= now + timedelta(seconds=1)


def test_period_days_preset():
    start, end = _parse_period(7, None, None)
    assert 6.5 <= (end - start).total_seconds() / 86400 <= 8.5


def test_period_rejects_reversed_range():
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _parse_period(None, '2026-08-20', '2026-08-01')


def test_unknown_category_rejected():
    with pytest.raises(ValidationError):
        ExpenseWriteRequest(category='hosting', title='x', amount=1)


def test_unsupported_currency_rejected():
    with pytest.raises(ValidationError):
        ExpenseWriteRequest(category='server', title='x', amount=1, currency='GBP')


def test_bad_link_rejected():
    with pytest.raises(ValidationError):
        ExpenseWriteRequest(category='ads', title='x', amount=1, link='javascript:alert(1)')


def test_negative_amount_rejected():
    with pytest.raises(ValidationError):
        ExpenseWriteRequest(category='ads', title='x', amount=-5)


@pytest.mark.asyncio
async def test_once_without_date_gets_today():
    """Разовый расход без даты не должен потеряться: подставляем сегодняшний день."""
    payload = ExpenseWriteRequest(category='ads', title='@channel', amount=1500)
    fields = await _payload_to_fields(payload)

    assert fields['spent_at'] is not None
    assert fields['amount_minor'] == 150_000


@pytest.mark.asyncio
async def test_foreign_once_snapshots_rate_and_monthly_does_not():
    """Оплаченный счёт фиксирует курс, регулярный расход считается по живому."""
    once = await _payload_to_fields(
        ExpenseWriteRequest(category='ai', title='Claude', amount=20, currency='USD', kind='once')
    )
    monthly = await _payload_to_fields(
        ExpenseWriteRequest(category='server', title='node', amount=6, currency='EUR', kind='monthly')
    )

    assert once['fx_rate_snapshot'] == 100.0
    assert monthly['fx_rate_snapshot'] is None


@pytest.mark.asyncio
async def test_rate_snapshot_survives_edit_of_unrelated_field():
    """Правка заметки не должна пересчитывать давно оплаченный счёт по новому курсу."""
    existing = ProfitExpense(
        category=ProfitExpenseCategory.AI.value,
        kind=ProfitExpenseKind.ONCE.value,
        title='Claude',
        amount_minor=2_000,
        currency='USD',
        fx_rate_snapshot=75.0,
    )
    payload = ExpenseWriteRequest(
        category='ai', title='Claude', amount=20, currency='USD', kind='once', notes='продлили'
    )

    fields = await _payload_to_fields(payload, existing=existing)

    assert fields['fx_rate_snapshot'] == 75.0


@pytest.mark.asyncio
async def test_changing_amount_refreshes_rate():
    """Сумма изменилась — это новый счёт, курс берём сегодняшний."""
    existing = ProfitExpense(
        category=ProfitExpenseCategory.AI.value,
        kind=ProfitExpenseKind.ONCE.value,
        title='Claude',
        amount_minor=2_000,
        currency='USD',
        fx_rate_snapshot=75.0,
    )
    payload = ExpenseWriteRequest(category='ai', title='Claude', amount=25, currency='USD', kind='once')

    fields = await _payload_to_fields(payload, existing=existing)

    assert fields['fx_rate_snapshot'] == 100.0


@pytest.mark.asyncio
async def test_rub_expense_never_stores_rate():
    fields = await _payload_to_fields(ExpenseWriteRequest(category='staff', title='Вася', amount=30_000))
    assert fields['fx_rate_snapshot'] is None


@pytest.mark.asyncio
async def test_naive_dates_become_utc():
    """Дата без таймзоны трактуется как UTC — иначе Postgres истолкует её по-своему."""
    payload = ExpenseWriteRequest(
        category='ads',
        title='@channel',
        amount=100,
        spent_at=datetime(2026, 8, 10, 12, 0),
    )
    fields = await _payload_to_fields(payload)

    assert fields['spent_at'].tzinfo is not None
    assert fields['spent_at'] == datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_end_bound_from_calendar_covers_whole_day():
    """Счёт «по 31 августа» должен включать сам 31-й, а не обрываться в полночь."""
    payload = ExpenseWriteRequest(
        category='cdn',
        title='Selectel',
        amount=3100,
        period_start=datetime(2026, 8, 1),
        period_end=datetime(2026, 8, 31),
    )
    fields = await _payload_to_fields(payload)

    assert fields['period_start'] == datetime(2026, 8, 1, tzinfo=UTC)
    assert fields['period_end'].hour == 23
    assert fields['period_end'].day == 31


@pytest.mark.asyncio
async def test_explicit_end_time_is_kept():
    """Явно указанное время верхней границы не переписывается на конец суток."""
    payload = ExpenseWriteRequest(
        category='server',
        title='node',
        amount=1000,
        kind='monthly',
        ended_at=datetime(2026, 8, 15, 9, 30, tzinfo=UTC),
    )
    fields = await _payload_to_fields(payload)

    assert fields['ended_at'] == datetime(2026, 8, 15, 9, 30, tzinfo=UTC)


def test_channel_handle_becomes_telegram_url():
    """@канал разворачивается в ссылку — иначе кабинет отрендерил бы относительный путь."""
    payload = ExpenseWriteRequest(category='ads', title='канал', amount=1, link='@vpn_news')
    assert payload.link == 'https://t.me/vpn_news'


def test_https_link_untouched():
    payload = ExpenseWriteRequest(
        category='ads', title='канал', amount=1, link='https://t.me/vpn_news'
    )
    assert payload.link == 'https://t.me/vpn_news'


def test_share_requires_percent():
    """Доля без процента бессмысленна — не даём её сохранить."""
    with pytest.raises(ValidationError):
        ExpenseWriteRequest(category='personal', title='Никита', kind='share')


def test_share_percent_capped_at_hundred():
    with pytest.raises(ValidationError):
        ExpenseWriteRequest(category='personal', title='Никита', kind='share', percent=120)


def test_percent_ignored_for_fixed_kinds():
    """Процент у обычной строки — недоразумение: молча обнуляем, чтобы не считался."""
    payload = ExpenseWriteRequest(category='personal', title='Вывод', amount=5000, percent=20)
    assert payload.percent is None


@pytest.mark.asyncio
async def test_share_stores_percent_and_no_amount():
    fields = await _payload_to_fields(
        ExpenseWriteRequest(category='personal', title='Никита', kind='share', percent=20)
    )

    assert fields['percent'] == 20
    assert fields['amount_minor'] == 0
    # Дата траты у доли не появляется: она действует окном, а не одним днём.
    assert fields['spent_at'] is None


def test_days_zero_means_all_time():
    """Кнопка «Всё время» шлёт days=0 — её нельзя спутать с «параметров нет»."""
    start, _ = _parse_period(0, None, None)
    assert start.year == 2020


def test_days_none_means_current_month():
    start, _ = _parse_period(None, None, None)
    assert start.day == 1 and start.month == datetime.now(UTC).month
