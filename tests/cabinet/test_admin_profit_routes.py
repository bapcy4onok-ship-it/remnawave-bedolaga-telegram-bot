"""Тесты админ-роутов «Чистая прибыль»: валидация, слияние при правке, сквозной HTTP."""

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.cabinet.routes.admin_profit import ExpenseWriteRequest, _parse_period, _payload_to_fields
from app.database.models import (
    Donation,
    GuestPurchase,
    ProfitExpense,
    ProfitExpenseCategory,
    ProfitExpenseKind,
    Transaction,
    User,
    WithdrawalRequest,
)
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


@pytest.mark.asyncio
async def test_payload_round_trips_into_the_database(monkeypatch):
    """Схема запроса → модель → БД → отчёт.

    Стык, который поодиночке не ловится: опечатка в имени поля видна только
    когда словарь из ``_payload_to_fields`` реально уходит в ``ProfitExpense``.
    """
    from datetime import timedelta

    from app.database.crud.profit_expense import create_expense, list_expenses
    from app.services.profit_service import calculate_profit_report
    from tests.fixtures.sqlite_memory import memory_session

    tables = [
        User.__table__,
        Transaction.__table__,
        Donation.__table__,
        GuestPurchase.__table__,
        WithdrawalRequest.__table__,
        ProfitExpense.__table__,
    ]

    async with memory_session(monkeypatch, tables) as session:
        for payload in (
            ExpenseWriteRequest(
                category='server', title='node-1', kind='monthly', amount=6, currency='EUR', group_name='Hetzner'
            ),
            ExpenseWriteRequest(category='ads', title='@channel', amount=1500, link='@channel'),
            ExpenseWriteRequest(category='personal', title='Никита', kind='share', percent=20),
        ):
            await create_expense(session, created_by=None, **await _payload_to_fields(payload))

        stored = await list_expenses(session)
        assert {e.title for e in stored} == {'node-1', '@channel', 'Никита'}
        assert next(e for e in stored if e.title == 'Никита').percent == 20

        now = datetime.now(UTC)
        report = await calculate_profit_report(session, now - timedelta(days=1), now)

    # Доход нулевой, поэтому доля равна нулю, а обычные расходы — нет.
    assert report['shares_total_kopeks'] == 0
    assert report['expenses']['total_kopeks'] > 0


# ── регрессии, найденные ревью ────────────────────────────────────────────────


def test_half_open_interval_rejected():
    """Интервал счёта задаётся парой: полузаполненный раньше сохранялся невидимкой."""
    with pytest.raises(ValidationError):
        ExpenseWriteRequest(category='cdn', title='Счёт', amount=5000, period_start=datetime(2026, 8, 1))
    with pytest.raises(ValidationError):
        ExpenseWriteRequest(category='cdn', title='Счёт', amount=5000, period_end=datetime(2026, 8, 31))


def test_reversed_interval_rejected():
    with pytest.raises(ValidationError):
        ExpenseWriteRequest(
            category='cdn',
            title='Счёт',
            amount=5000,
            period_start=datetime(2026, 8, 31),
            period_end=datetime(2026, 8, 1),
        )


def test_reversed_active_window_rejected():
    with pytest.raises(ValidationError):
        ExpenseWriteRequest(
            category='server',
            title='node',
            kind='monthly',
            amount=3000,
            started_at=datetime(2026, 8, 31),
            ended_at=datetime(2026, 8, 1),
        )


@pytest.mark.asyncio
async def test_patch_keeps_fields_that_were_not_sent():
    """PATCH — это правка, а не замена.

    Диалог серверов шлёт только цену; раньше это обнуляло «действует с/по»
    и заметки, и снятый в июне сервер снова начислялся вечно.
    """
    from app.cabinet.routes.admin_profit import ExpensePatchRequest, _expense_to_write_fields

    existing = ProfitExpense(
        category=ProfitExpenseCategory.SERVER.value,
        kind=ProfitExpenseKind.MONTHLY.value,
        title='node-1',
        group_name='Aeza',
        amount_minor=300_000,
        currency='RUB',
        started_at=datetime(2026, 3, 1, tzinfo=UTC),
        ended_at=datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC),
        notes='оплачен до июля',
        node_uuid='node-uuid-1',
        sort_order=5,
        is_active=True,
    )

    patch = ExpensePatchRequest(amount=3500)
    merged = ExpenseWriteRequest(**{**_expense_to_write_fields(existing), **patch.model_dump(exclude_unset=True)})
    fields = await _payload_to_fields(merged, existing=existing)

    assert fields['amount_minor'] == 350_000
    assert fields['started_at'] == datetime(2026, 3, 1, tzinfo=UTC)
    assert fields['ended_at'] == datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
    assert fields['notes'] == 'оплачен до июля'
    assert fields['node_uuid'] == 'node-uuid-1'
    assert fields['sort_order'] == 5
    assert fields['group_name'] == 'Aeza'


@pytest.mark.asyncio
async def test_patch_validates_the_merged_row_not_the_fragment():
    """Правила проверяются на итоговом состоянии строки, а не на куске запроса."""
    from app.cabinet.routes.admin_profit import ExpensePatchRequest, _expense_to_write_fields

    existing = ProfitExpense(
        category=ProfitExpenseCategory.PERSONAL.value,
        kind=ProfitExpenseKind.SHARE.value,
        title='Никита',
        amount_minor=0,
        currency='RUB',
        percent=20,
        is_active=True,
    )

    # Меняем только процент — доля остаётся валидной.
    merged = ExpenseWriteRequest(
        **{
            **_expense_to_write_fields(existing),
            **ExpensePatchRequest(percent=25).model_dump(exclude_unset=True),
        }
    )
    assert merged.kind == 'share'
    assert merged.percent == 25

    # А вот переключение доли в разовый расход без суммы обязано обнулить процент.
    switched = ExpenseWriteRequest(
        **{
            **_expense_to_write_fields(existing),
            **ExpensePatchRequest(kind='once', amount=1000).model_dump(exclude_unset=True),
        }
    )
    assert switched.percent is None


def test_patch_rejects_unknown_fields():
    """extra='forbid': опечатка в имени поля не должна молча ничего не делать."""
    from app.cabinet.routes.admin_profit import ExpensePatchRequest

    with pytest.raises(ValidationError):
        ExpensePatchRequest(amout=3500)


@pytest.mark.asyncio
async def test_node_expense_is_upserted_not_duplicated(monkeypatch):
    """Повторный POST по той же ноде правит строку, а не создаёт вторую.

    Быстрый ввод цен успевал отправить второй POST до прихода рефетча,
    и аренда считалась дважды — причём дубль не был виден в диалоге серверов.
    """
    from sqlalchemy.exc import IntegrityError

    from app.database.crud.profit_expense import create_expense, get_expense_by_node, list_expenses, update_expense
    from tests.fixtures.sqlite_memory import memory_session

    async with memory_session(monkeypatch, [ProfitExpense.__table__]) as session:
        payload = ExpenseWriteRequest(
            category='server', title='node-1', kind='monthly', amount=500, node_uuid='uuid-1'
        )
        fields = await _payload_to_fields(payload)
        await create_expense(session, created_by=None, **fields)

        # Повтор — тот путь, что теперь в роуте: сначала ищем строку ноды.
        existing = await get_expense_by_node(session, 'uuid-1')
        assert existing is not None
        second = await _payload_to_fields(
            ExpenseWriteRequest(
                category='server', title='node-1', kind='monthly', amount=700, currency='USD', node_uuid='uuid-1'
            )
        )
        await update_expense(session, existing, **second)

        rows = await list_expenses(session)
        assert len(rows) == 1
        assert rows[0].amount_minor == 70_000
        assert rows[0].currency == 'USD'

        # А в обход апсерта БД сама не даст завести дубль.
        with pytest.raises(IntegrityError):
            await create_expense(session, created_by=None, **fields)


# ── регрессии, найденные аудитом исправлений ─────────────────────────────────


def test_mixing_aware_and_naive_dates_does_not_explode():
    """Дата из БД (aware) и дата от клиента (naive) сравнимы.

    Голое сравнение бросало TypeError, а pydantic заворачивает только
    ValueError — клиент получал 500 на правке одного поля пары.
    """
    payload = ExpenseWriteRequest(
        category='server',
        title='node',
        kind='monthly',
        amount=1,
        started_at=datetime(2026, 3, 1, tzinfo=UTC),
        ended_at=datetime(2026, 6, 30),
    )
    assert payload.ended_at is not None

    with pytest.raises(ValidationError):
        ExpenseWriteRequest(
            category='server',
            title='node',
            kind='monthly',
            amount=1,
            started_at=datetime(2026, 6, 30, tzinfo=UTC),
            ended_at=datetime(2026, 3, 1),
        )


@pytest.mark.asyncio
async def test_upsert_by_node_keeps_untouched_fields():
    """POST по существующей ноде — правка, а не замена.

    Апсерт добавили ради дублей, но он собирал полный словарь дефолтов и сносил
    «действует с/по» и заметки — тот самый вред, от которого лечили PATCH.
    """
    from app.cabinet.routes.admin_profit import _expense_to_write_fields

    existing = ProfitExpense(
        category=ProfitExpenseCategory.SERVER.value,
        kind=ProfitExpenseKind.MONTHLY.value,
        title='node-1',
        group_name='Aeza',
        amount_minor=300_000,
        currency='RUB',
        started_at=datetime(2026, 3, 1, tzinfo=UTC),
        ended_at=datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC),
        notes='снят в июне',
        color='#ff0000',
        sort_order=3,
        node_uuid='uuid-1',
        is_active=True,
    )

    # То, что шлёт диалог серверов: только цена и опознание строки.
    incoming = ExpenseWriteRequest(
        category='server', title='node-1', kind='monthly', amount=3500, group_name='Aeza', node_uuid='uuid-1'
    )
    merged = ExpenseWriteRequest(
        **{**_expense_to_write_fields(existing), **incoming.model_dump(exclude_unset=True)}
    )
    fields = await _payload_to_fields(merged, existing=existing)

    assert fields['amount_minor'] == 350_000
    assert fields['started_at'] == datetime(2026, 3, 1, tzinfo=UTC)
    assert fields['ended_at'] == datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)
    assert fields['notes'] == 'снят в июне'
    assert fields['color'] == '#ff0000'
    assert fields['sort_order'] == 3


@pytest.mark.asyncio
async def test_legacy_half_interval_row_is_repaired_on_edit():
    """Строку с одиноким period_start можно править, а не только смотреть.

    Валидация слитого состояния делала любой PATCH такой строки 422 —
    из-за поля, которого запрос даже не касался.
    """
    from app.cabinet.routes.admin_profit import ExpensePatchRequest, _expense_to_write_fields

    legacy = ProfitExpense(
        category=ProfitExpenseCategory.SERVER.value,
        kind=ProfitExpenseKind.MONTHLY.value,
        title='node-legacy',
        amount_minor=300_000,
        currency='RUB',
        period_start=datetime(2026, 6, 1, tzinfo=UTC),
        period_end=None,
        node_uuid='uuid-legacy',
        is_active=True,
    )

    merged = ExpenseWriteRequest(
        **{
            **_expense_to_write_fields(legacy),
            **ExpensePatchRequest(amount=3500).model_dump(exclude_unset=True),
        }
    )

    assert merged.period_start is None
    assert merged.period_end is None
    # Одинокое начало интервала не потеряно — стало датой траты.
    assert merged.spent_at == datetime(2026, 6, 1, tzinfo=UTC)


# ── сквозные проверки по HTTP ────────────────────────────────────────────────
#
# Всё выше зовёт хелперы напрямую и потому не видит того, что происходит между
# запросом и ответом: слияние при PATCH, обёртку ошибок валидации, сериализацию.
# Ровно там и жил TypeError, улетавший клиенту как 500.


@contextlib.asynccontextmanager
async def _profit_client(monkeypatch):
    """Приложение с одним профит-роутером, реальной сессией и открытыми правами."""
    from unittest.mock import AsyncMock

    import httpx
    from fastapi import FastAPI

    from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
    from app.cabinet.routes.admin_profit import router
    from app.services.permission_service import PermissionService
    from tests.fixtures.sqlite_memory import memory_session

    # Проверку права и запись в аудит глушим: обе к фиче не относятся,
    # а таблица аудита с BigInteger-ключом на sqlite не автоинкрементится.
    monkeypatch.setattr(
        PermissionService, 'check_permission', AsyncMock(return_value=(True, None)), raising=False
    )
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock(return_value=None), raising=False)

    async with memory_session(monkeypatch, [ProfitExpense.__table__]) as session:
        app = FastAPI()
        app.include_router(router, prefix='/cabinet')
        app.dependency_overrides[get_cabinet_db] = lambda: session
        app.dependency_overrides[get_current_cabinet_user] = lambda: SimpleNamespace(id=1, telegram_id=1)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            yield client, session


@pytest.mark.asyncio
async def test_http_patch_of_one_date_does_not_return_500(monkeypatch):
    """Правка одной даты пары — 200, а не 500.

    В БД дата лежит с таймзоной, клиент шлёт без неё; сравнение до приведения
    к UTC бросало TypeError, который pydantic не заворачивает.
    """
    async with _profit_client(monkeypatch) as (client, _session):
        created = await client.post(
            '/cabinet/admin/profit/expenses',
            json={
                'category': 'server',
                'title': 'node-1',
                'kind': 'monthly',
                'amount': 3000,
                'started_at': '2026-03-01T00:00:00',
                'notes': 'заметка',
            },
        )
        assert created.status_code == 201, created.text
        expense_id = created.json()['id']

        patched = await client.patch(
            f'/cabinet/admin/profit/expenses/{expense_id}',
            json={'ended_at': '2026-06-30T00:00:00'},
        )

    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body['ended_at'].startswith('2026-06-30T23:59:59')
    # Непереданные поля на месте.
    assert body['started_at'].startswith('2026-03-01')
    assert body['notes'] == 'заметка'
    assert body['amount'] == 3000


@pytest.mark.asyncio
async def test_http_bad_dates_are_422_not_500(monkeypatch):
    """Вывернутый интервал — понятная ошибка валидации, а не падение."""
    async with _profit_client(monkeypatch) as (client, _session):
        response = await client.post(
            '/cabinet/admin/profit/expenses',
            json={
                'category': 'cdn',
                'title': 'Счёт',
                'amount': 5000,
                'period_start': '2026-08-31T00:00:00',
                'period_end': '2026-08-01T00:00:00',
            },
        )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_http_repeated_post_for_a_node_updates_instead_of_duplicating(monkeypatch):
    """Повторный POST по ноде правит строку и не теряет то, чего в нём не было."""
    async with _profit_client(monkeypatch) as (client, _session):
        first = await client.post(
            '/cabinet/admin/profit/expenses',
            json={
                'category': 'server',
                'title': 'node-1',
                'kind': 'monthly',
                'amount': 3000,
                'node_uuid': 'uuid-1',
                'started_at': '2026-03-01T00:00:00',
                'notes': 'снят в июне',
            },
        )
        assert first.status_code == 201, first.text

        # Ровно то, что шлёт диалог серверов: только цена и опознание строки.
        second = await client.post(
            '/cabinet/admin/profit/expenses',
            json={
                'category': 'server',
                'title': 'node-1',
                'kind': 'monthly',
                'amount': 3500,
                'node_uuid': 'uuid-1',
            },
        )
        assert second.status_code == 201, second.text

        listed = await client.get('/cabinet/admin/profit/expenses')

    rows = listed.json()
    assert len(rows) == 1, rows
    assert rows[0]['amount'] == 3500
    assert rows[0]['started_at'].startswith('2026-03-01')
    assert rows[0]['notes'] == 'снят в июне'


@pytest.mark.asyncio
async def test_http_share_requires_percent(monkeypatch):
    async with _profit_client(monkeypatch) as (client, _session):
        bad = await client.post(
            '/cabinet/admin/profit/expenses',
            json={'category': 'personal', 'title': 'Никита', 'kind': 'share'},
        )
        good = await client.post(
            '/cabinet/admin/profit/expenses',
            json={'category': 'personal', 'title': 'Никита', 'kind': 'share', 'percent': 20},
        )

    assert bad.status_code == 422, bad.text
    assert good.status_code == 201, good.text
    assert good.json()['percent'] == 20


@pytest.mark.asyncio
async def test_http_concurrent_post_for_a_node_does_not_500(monkeypatch):
    """Гонка двух POST по одной ноде: второй получает ответ, а не ошибку.

    Проверка «строка уже есть» и вставка не атомарны, поэтому оба запроса могли
    дойти до INSERT — уникальный индекс отбивал второй, и клиент видел 500.
    """
    async with _profit_client(monkeypatch) as (client, session):
        body = {
            'category': 'server',
            'title': 'node-1',
            'kind': 'monthly',
            'amount': 3000,
            'node_uuid': 'uuid-race',
        }

        # Имитируем проигрыш гонки: строку создаёт «кто-то другой» ровно между
        # проверкой и вставкой нашего запроса.
        from app.cabinet.routes import admin_profit
        from app.database.crud.profit_expense import create_expense

        original = admin_profit.get_expense_by_node
        calls = {'n': 0}

        async def racing_lookup(db, node_uuid):
            calls['n'] += 1
            if calls['n'] == 1:
                # Первый взгляд — пусто, но соперник успевает вставить строку.
                await create_expense(
                    db,
                    created_by=None,
                    category='server',
                    kind='monthly',
                    title='node-1',
                    amount_minor=100_000,
                    currency='RUB',
                    node_uuid=node_uuid,
                    notes='от соперника',
                )
                return None
            return await original(db, node_uuid)

        monkeypatch.setattr(admin_profit, 'get_expense_by_node', racing_lookup)

        response = await client.post('/cabinet/admin/profit/expenses', json=body)
        listed = await client.get('/cabinet/admin/profit/expenses')

    assert response.status_code == 201, response.text
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]['amount'] == 3000
    assert rows[0]['notes'] == 'от соперника'
