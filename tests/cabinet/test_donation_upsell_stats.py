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
    db.execute = AsyncMock(return_value=_rows_result([('menu', 50000), ('topup_upsell', 7000)]))

    result = await donation_crud.get_donations_by_source_kopeks(db)

    assert result == {'menu': 50000, 'topup_upsell': 7000}


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


@pytest.mark.asyncio
async def test_stats_endpoint_returns_source_and_funnel(monkeypatch):
    async def fake_execute(*args, **kwargs):
        raise AssertionError('не должно вызываться напрямую')

    db = AsyncMock()
    db.execute = fake_execute

    monkeypatch.setattr(donation_crud, 'get_total_donations_kopeks', AsyncMock(return_value=57000))
    monkeypatch.setattr(donation_crud, 'count_distinct_donors', AsyncMock(return_value=9))
    monkeypatch.setattr(donation_crud, 'get_donation_leaderboard', AsyncMock(return_value=[]))
    monkeypatch.setattr(
        donation_crud,
        'get_donations_by_source_kopeks',
        AsyncMock(return_value={'menu': 50000, 'topup_upsell': 7000}),
    )
    monkeypatch.setattr(
        pledge_crud,
        'get_upsell_funnel',
        AsyncMock(return_value={'shown': 102, 'accepted': 12, 'applied': 6, 'declined': 90}),
    )

    response = await stats_routes.get_donations_stats(
        days=None, start_date=None, end_date=None, limit=20, admin=MagicMock(), db=db
    )

    assert response.total_amount_kopeks == 57000
    assert response.by_source == {'menu': 50000, 'topup_upsell': 7000}
    assert response.upsell_funnel['applied'] == 6
