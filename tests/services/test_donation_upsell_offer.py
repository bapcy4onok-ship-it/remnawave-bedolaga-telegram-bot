"""Когда апселл показывается, а когда молчит; и что именно предлагает."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.donation_upsell_service as upsell
from app.config import settings


def _user(user_id: int = 42, restricted: bool = False):
    user = MagicMock()
    user.id = user_id
    user.restriction_topup = restricted
    return user


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_AMOUNT', 10, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_PRESETS', '25,50,100', raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_COOLDOWN_DAYS', 0, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_MIN_TOPUP', 0, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_SKIP_IF_DONATED', False, raising=False)
    monkeypatch.setattr(upsell.pledge_crud, 'has_recent_decision', AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_offer_built_for_regular_topup():
    offer = await upsell.build_offer(
        AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='yookassa', method_max_kopeks=10_000_000
    )

    assert offer is not None
    assert offer.tip_kopeks == 1000
    assert offer.total_kopeks == 51000
    assert offer.presets_kopeks == [2500, 5000, 10000]


@pytest.mark.asyncio
async def test_no_offer_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', False, raising=False)

    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='yookassa')

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_when_donations_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', False, raising=False)

    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='yookassa')

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_for_restricted_user():
    offer = await upsell.build_offer(
        AsyncMock(), _user(restricted=True), topup_amount_kopeks=50000, payment_method='yookassa'
    )

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_for_tribute():
    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='tribute')

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_below_min_topup(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_MIN_TOPUP', 300, raising=False)

    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=10000, payment_method='yookassa')

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_when_tip_does_not_fit_method_limit():
    offer = await upsell.build_offer(
        AsyncMock(), _user(), topup_amount_kopeks=100000, payment_method='yookassa', method_max_kopeks=100000
    )

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_within_cooldown(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_COOLDOWN_DAYS', 7, raising=False)
    monkeypatch.setattr(upsell.pledge_crud, 'has_recent_decision', AsyncMock(return_value=True))

    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='yookassa')

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_when_user_already_donated(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_SKIP_IF_DONATED', True, raising=False)
    monkeypatch.setattr(upsell, 'get_user_donation_total_kopeks', AsyncMock(return_value=5000))

    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='yookassa')

    assert offer is None


def test_max_tip_respects_method_limit():
    assert upsell.resolve_max_tip_kopeks(topup_amount_kopeks=90000, method_max_kopeks=100000) == 10000
    assert upsell.resolve_max_tip_kopeks(topup_amount_kopeks=90000, method_max_kopeks=None) == upsell.MAX_TIP_KOPEKS
    assert upsell.resolve_max_tip_kopeks(topup_amount_kopeks=100000, method_max_kopeks=100000) == 0


@pytest.mark.asyncio
async def test_register_pledge_supersedes_previous(monkeypatch):
    db = AsyncMock()
    supersede = AsyncMock(return_value=1)
    create = AsyncMock(return_value=MagicMock(id=3))
    monkeypatch.setattr(upsell.pledge_crud, 'supersede_pending_pledges', supersede)
    monkeypatch.setattr(upsell.pledge_crud, 'create_pledge', create)

    await upsell.register_pledge(
        db, _user(), tip_kopeks=1000, topup_amount_kopeks=50000, payment_method='yookassa', source='bot'
    )

    supersede.assert_awaited_once()
    assert create.await_args.kwargs['tip_kopeks'] == 1000
    assert create.await_args.kwargs['source'] == 'bot'
