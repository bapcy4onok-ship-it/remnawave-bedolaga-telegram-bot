"""Статистика: донаты разложены по источнику, воронка апселла считается."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.cabinet.routes.admin_sales_stats as stats_routes
from app.database.crud import donation as donation_crud, donation_pledge as pledge_crud


def _rows_result(rows):
    result = MagicMock()
    result.all = MagicMock(return_value=list(rows))
    return result


@pytest.mark.asyncio
async def test_donations_by_source_maps_rows():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_rows_result([('menu', 50000, 2), ('topup_upsell', 7000, 4)]))

    result = await donation_crud.get_donations_by_source(db)

    assert result == {
        'menu': {'amount_kopeks': 50000, 'count': 2},
        'topup_upsell': {'amount_kopeks': 7000, 'count': 4},
    }


@pytest.mark.asyncio
async def test_upsell_funnel_counts_statuses():
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=_rows_result([('pending', 4), ('applied', 6), ('declined', 90), ('superseded', 2)])
    )

    funnel = await pledge_crud.get_upsell_funnel(db)

    assert funnel['shown'] == 102
    assert funnel['accepted'] == 12  # pending + applied + superseded
    assert funnel['applied'] == 6
    assert funnel['declined'] == 90


def _patch_donation_crud(monkeypatch, **overrides):
    """Мокает все CRUD-функции, которые дёргает эндпоинт, чтобы не ходить в БД."""
    defaults = {
        'get_total_donations_kopeks': 57000,
        'count_donations': 6,
        'count_distinct_donors': 9,
        'get_donation_leaderboard': [],
        'get_donations_by_source': {
            'menu': {'amount_kopeks': 50000, 'count': 2},
            'topup_upsell': {'amount_kopeks': 7000, 'count': 4},
        },
        'get_donations_daily_series': [{'date': '2026-08-23', 'count': 6, 'amount_kopeks': 57000}],
        'get_donations_daily_by_source': [
            {'date': '2026-08-23', 'source': 'menu', 'amount_kopeks': 50000},
            {'date': '2026-08-23', 'source': 'topup_upsell', 'amount_kopeks': 7000},
        ],
    }
    defaults.update(overrides)
    mocks = {}
    for name, value in defaults.items():
        mocks[name] = AsyncMock(return_value=value)
        monkeypatch.setattr(donation_crud, name, mocks[name])
    mocks['get_upsell_funnel'] = AsyncMock(return_value={'shown': 102, 'accepted': 12, 'applied': 6, 'declined': 90})
    monkeypatch.setattr(pledge_crud, 'get_upsell_funnel', mocks['get_upsell_funnel'])
    return mocks


def _db_without_queries():
    async def fake_execute(*args, **kwargs):
        raise AssertionError('не должно вызываться напрямую')

    db = AsyncMock()
    db.execute = fake_execute
    return db


@pytest.mark.asyncio
async def test_stats_endpoint_returns_source_and_funnel(monkeypatch):
    _patch_donation_crud(monkeypatch)

    response = await stats_routes.get_donations_stats(
        days=None, start_date=None, end_date=None, limit=20, admin=MagicMock(), db=_db_without_queries()
    )

    assert response.total_amount_kopeks == 57000
    assert response.by_source == {'menu': 50000, 'topup_upsell': 7000}
    assert response.upsell_funnel['applied'] == 6


@pytest.mark.asyncio
async def test_stats_endpoint_returns_counts_avg_and_daily(monkeypatch):
    _patch_donation_crud(monkeypatch)

    response = await stats_routes.get_donations_stats(
        days=None, start_date=None, end_date=None, limit=20, admin=MagicMock(), db=_db_without_queries()
    )

    assert response.donations_count == 6
    assert response.donors_count == 9
    assert response.avg_donation_kopeks == 9500  # 57000 // 6, целочисленно
    # Формат by_source не менялся: плоский {источник: сумма}, на нём завязан выложенный фронт.
    assert response.by_source == {'menu': 50000, 'topup_upsell': 7000}
    assert all(isinstance(value, int) for value in response.by_source.values())
    assert response.by_source_count == {'menu': 2, 'topup_upsell': 4}
    assert [(row.date, row.count, row.amount_kopeks) for row in response.daily] == [('2026-08-23', 6, 57000)]
    assert [(row.date, row.source, row.amount_kopeks) for row in response.daily_by_source] == [
        ('2026-08-23', 'menu', 50000),
        ('2026-08-23', 'topup_upsell', 7000),
    ]


@pytest.mark.asyncio
async def test_stats_endpoint_avg_is_zero_without_donations(monkeypatch):
    _patch_donation_crud(
        monkeypatch,
        get_total_donations_kopeks=0,
        count_donations=0,
        count_distinct_donors=0,
        get_donations_by_source={},
        get_donations_daily_series=[],
        get_donations_daily_by_source=[],
    )

    response = await stats_routes.get_donations_stats(
        days=30, start_date=None, end_date=None, limit=20, admin=MagicMock(), db=_db_without_queries()
    )

    assert response.donations_count == 0
    assert response.avg_donation_kopeks == 0
    assert response.by_source == {}
    assert response.by_source_count == {}
    assert response.daily == []
    assert response.daily_by_source == []


@pytest.mark.asyncio
async def test_stats_endpoint_passes_period_to_every_query(monkeypatch):
    """Период обязан доехать до КАЖДОГО запроса: иначе карточка и график покажут разные окна."""
    mocks = _patch_donation_crud(monkeypatch)

    await stats_routes.get_donations_stats(
        days=30, start_date=None, end_date=None, limit=20, admin=MagicMock(), db=_db_without_queries()
    )

    for name, mock in mocks.items():
        assert mock.await_args.kwargs['start'] is not None, name
        assert mock.await_args.kwargs['end'] is not None, name


@pytest.mark.asyncio
async def test_stats_endpoint_without_period_asks_for_all_time(monkeypatch):
    """days=None и без дат — статистика за всё время, а не за окно по умолчанию."""
    mocks = _patch_donation_crud(monkeypatch)

    await stats_routes.get_donations_stats(
        days=None, start_date=None, end_date=None, limit=20, admin=MagicMock(), db=_db_without_queries()
    )

    for name, mock in mocks.items():
        assert mock.await_args.kwargs['start'] is None, name
        assert mock.await_args.kwargs['end'] is None, name
