"""CRUD обещаний надбавки: сессия замокана, проверяем поведение, не SQL."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.crud import donation_pledge as crud
from app.database.models import DonationPledgeStatus


def _db(execute_results=None):
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(side_effect=list(execute_results or []))
    return db


def _rowcount_result(rowcount: int):
    result = MagicMock()
    result.rowcount = rowcount
    return result


def _scalars_result(rows):
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=list(rows))
    result.scalars = MagicMock(return_value=scalars)
    return result


def _scalar_result(value):
    result = MagicMock()
    result.scalar = MagicMock(return_value=value)
    return result


@pytest.mark.asyncio
async def test_create_pledge_sets_total_and_expiry():
    db = _db()

    pledge = await crud.create_pledge(
        db,
        user_id=42,
        tip_kopeks=1000,
        topup_amount_kopeks=50000,
        payment_method='yookassa',
        source='bot',
        ttl_hours=72,
    )

    assert pledge.expected_total_kopeks == 51000
    assert pledge.status == DonationPledgeStatus.PENDING
    assert pledge.expires_at is not None
    assert pledge.expires_at > datetime.now(UTC)
    db.add.assert_called_once()
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_declined_pledge_has_no_tip_and_no_expiry():
    db = _db()

    pledge = await crud.create_pledge(
        db,
        user_id=42,
        tip_kopeks=0,
        topup_amount_kopeks=50000,
        payment_method='yookassa',
        source='cabinet',
        ttl_hours=72,
        status=DonationPledgeStatus.DECLINED,
    )

    assert pledge.tip_kopeks == 0
    assert pledge.expected_total_kopeks == 50000
    assert pledge.status == DonationPledgeStatus.DECLINED
    assert pledge.expires_at is None


@pytest.mark.asyncio
async def test_mark_pledge_applied_returns_false_when_already_applied():
    db = _db([_rowcount_result(0)])

    applied = await crud.mark_pledge_applied(db, pledge_id=7, transaction_id=99)

    assert applied is False
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_pledge_applied_returns_true_on_first_call():
    db = _db([_rowcount_result(1)])

    applied = await crud.mark_pledge_applied(db, pledge_id=7, transaction_id=99)

    assert applied is True


@pytest.mark.asyncio
async def test_list_pending_pledges_returns_rows():
    row = MagicMock(id=5)
    db = _db([_scalars_result([row])])

    pledges = await crud.list_pending_pledges(db, user_id=42)

    assert pledges == [row]


@pytest.mark.asyncio
async def test_has_recent_decision_false_when_cooldown_disabled():
    db = _db()

    assert await crud.has_recent_decision(db, user_id=42, days=0) is False
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_has_recent_decision_true_when_row_found():
    db = _db([_scalar_result(1)])

    assert await crud.has_recent_decision(db, user_id=42, days=7) is True
