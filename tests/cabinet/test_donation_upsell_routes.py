"""Кабинетное API апселла: надбавка увеличивает платёж и создаёт обещание."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.cabinet.routes.balance as balance_routes
from app.cabinet.schemas.balance import TopUpRequest
from app.config import settings


def _user():
    user = MagicMock()
    user.id = 42
    user.telegram_id = 111
    user.username = 'donor'
    user.language = 'ru'
    user.restriction_topup = False
    return user


@pytest.mark.asyncio
async def test_upsell_settings_endpoint_reports_availability(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_AMOUNT', 10, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_PRESETS', '25,50', raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_MIN_TOPUP', 100, raising=False)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=True))

    response = await balance_routes.get_donation_upsell(user=_user(), db=AsyncMock())

    assert response.available is True
    assert response.tip_kopeks == 1000
    assert response.presets_kopeks == [2500, 5000]
    assert response.min_topup_kopeks == 10000


@pytest.mark.asyncio
async def test_upsell_settings_endpoint_unavailable(monkeypatch):
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=False))

    response = await balance_routes.get_donation_upsell(user=_user(), db=AsyncMock())

    assert response.available is False


def test_topup_request_accepts_tip():
    request = TopUpRequest(
        amount_kopeks=50000,
        payment_method='yookassa',
        donation_tip_kopeks=1000,
        donation_upsell_shown=True,
    )

    assert request.donation_tip_kopeks == 1000
    assert request.donation_upsell_shown is True


@pytest.mark.asyncio
async def test_apply_tip_increases_amount_and_registers_pledge(monkeypatch):
    register_pledge = AsyncMock(return_value=MagicMock(id=1))
    monkeypatch.setattr(balance_routes, 'register_pledge', register_pledge)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=True))

    request = TopUpRequest(
        amount_kopeks=50000,
        payment_method='yookassa',
        donation_tip_kopeks=1000,
        donation_upsell_shown=True,
    )

    patched, tip, pledge = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert patched.amount_kopeks == 51000
    assert tip == 1000
    assert pledge is not None
    assert register_pledge.await_args.kwargs['tip_kopeks'] == 1000
    assert register_pledge.await_args.kwargs['source'] == 'cabinet'


@pytest.mark.asyncio
async def test_declined_upsell_records_decline_without_changing_amount(monkeypatch):
    register_decline = AsyncMock()
    monkeypatch.setattr(balance_routes, 'register_decline', register_decline)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=True))

    request = TopUpRequest(
        amount_kopeks=50000,
        payment_method='yookassa',
        donation_tip_kopeks=0,
        donation_upsell_shown=True,
    )

    patched, tip, pledge = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert patched.amount_kopeks == 50000
    assert tip == 0
    assert pledge is None
    register_decline.assert_awaited_once()


@pytest.mark.asyncio
async def test_tip_ignored_when_upsell_unavailable(monkeypatch):
    register_pledge = AsyncMock()
    monkeypatch.setattr(balance_routes, 'register_pledge', register_pledge)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=False))

    request = TopUpRequest(amount_kopeks=50000, payment_method='yookassa', donation_tip_kopeks=1000)

    patched, tip, _pledge = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert patched.amount_kopeks == 50000
    assert tip == 0
    register_pledge.assert_not_awaited()


@pytest.mark.asyncio
async def test_tip_never_added_to_a_donation(monkeypatch):
    """Само пожертвование надбавкой не сопровождаем."""
    register_pledge = AsyncMock()
    monkeypatch.setattr(balance_routes, 'register_pledge', register_pledge)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=True))

    request = TopUpRequest(
        amount_kopeks=50000,
        payment_method='yookassa',
        purpose='donation',
        donation_tip_kopeks=1000,
        donation_upsell_shown=True,
    )

    patched, tip, _pledge = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert patched.amount_kopeks == 50000
    assert tip == 0
    register_pledge.assert_not_awaited()


@pytest.mark.asyncio
async def test_tip_never_added_for_tribute(monkeypatch):
    """У Tribute сумму задаёт его же страница — обещание было бы неисполнимо."""
    register_pledge = AsyncMock()
    monkeypatch.setattr(balance_routes, 'register_pledge', register_pledge)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=True))

    request = TopUpRequest(
        amount_kopeks=50000,
        payment_method='tribute',
        donation_tip_kopeks=1000,
        donation_upsell_shown=True,
    )

    patched, tip, _pledge = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert patched.amount_kopeks == 50000
    assert tip == 0
    register_pledge.assert_not_awaited()


@pytest.mark.asyncio
async def test_tip_clamped_to_method_limit(monkeypatch):
    """Надбавка обрезается под максимум способа, а не выкидывает платёж за границу."""
    register_pledge = AsyncMock(return_value=MagicMock(id=2))
    monkeypatch.setattr(balance_routes, 'register_pledge', register_pledge)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=True))

    request = TopUpRequest(
        amount_kopeks=99000,
        payment_method='yookassa',
        donation_tip_kopeks=5000,
        donation_upsell_shown=True,
    )

    patched, tip, _pledge = await balance_routes._apply_donation_tip(
        AsyncMock(), _user(), request, method_max_kopeks=100000
    )

    assert tip == 1000
    assert patched.amount_kopeks == 100000


@pytest.mark.asyncio
async def test_decline_not_recorded_below_min_topup(monkeypatch):
    """Ниже порога экрана быть не должно — значит и отказу взяться неоткуда."""
    register_decline = AsyncMock()
    monkeypatch.setattr(balance_routes, 'register_decline', register_decline)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=True))
    monkeypatch.setattr(balance_routes, 'cancel_pending_pledges', AsyncMock())
    monkeypatch.setattr(settings, 'DONATION_UPSELL_MIN_TOPUP', 100, raising=False)

    request = TopUpRequest(
        amount_kopeks=5000,
        payment_method='yookassa',
        donation_tip_kopeks=0,
        donation_upsell_shown=True,
    )

    patched, tip, pledge = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert patched.amount_kopeks == 5000
    assert tip == 0
    assert pledge is None
    register_decline.assert_not_awaited()


@pytest.mark.asyncio
async def test_topup_without_upsell_cancels_stale_pledge(monkeypatch):
    """Пополнение мимо апселла (старый бандл, Tribute, порог) гасит прежнее обещание."""
    cancel = AsyncMock(return_value=1)
    monkeypatch.setattr(balance_routes, 'cancel_pending_pledges', cancel)
    db = AsyncMock()
    user = _user()

    request = TopUpRequest(amount_kopeks=50000, payment_method='yookassa')

    await balance_routes._apply_donation_tip(db, user, request)

    cancel.assert_awaited_once_with(db, user)


@pytest.mark.asyncio
async def test_donation_purpose_keeps_pending_pledge(monkeypatch):
    """Пожертвование из меню — не пополнение: чужое обещание гасить нельзя."""
    cancel = AsyncMock()
    monkeypatch.setattr(balance_routes, 'cancel_pending_pledges', cancel)

    # Настоящий донат приходит без полей апселла — модалку на нём не показывают.
    request = TopUpRequest(amount_kopeks=50000, payment_method='yookassa', purpose='donation')

    _patched, tip, pledge = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert (tip, pledge) == (0, None)
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_excluded_method_cancels_stale_pledge(monkeypatch):
    """Tribute подбирается по сумме, а не по способу — старое обещание к нему не относится."""
    cancel = AsyncMock(return_value=1)
    monkeypatch.setattr(balance_routes, 'cancel_pending_pledges', cancel)

    request = TopUpRequest(
        amount_kopeks=50000,
        payment_method='tribute',
        donation_tip_kopeks=1000,
        donation_upsell_shown=True,
    )

    _patched, tip, pledge = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert (tip, pledge) == (0, None)
    cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_unavailable_upsell_cancels_stale_pledge(monkeypatch):
    """Cooldown/запрет пополнений — экрана не было, обещание гасим."""
    cancel = AsyncMock(return_value=1)
    monkeypatch.setattr(balance_routes, 'cancel_pending_pledges', cancel)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=False))

    request = TopUpRequest(
        amount_kopeks=50000,
        payment_method='yookassa',
        donation_tip_kopeks=1000,
        donation_upsell_shown=True,
    )

    _patched, tip, pledge = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert (tip, pledge) == (0, None)
    cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_min_topup_branch_cancels_stale_pledge(monkeypatch):
    cancel = AsyncMock(return_value=1)
    monkeypatch.setattr(balance_routes, 'cancel_pending_pledges', cancel)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=True))
    monkeypatch.setattr(settings, 'DONATION_UPSELL_MIN_TOPUP', 100, raising=False)

    request = TopUpRequest(
        amount_kopeks=5000,
        payment_method='yookassa',
        donation_tip_kopeks=1000,
        donation_upsell_shown=True,
    )

    await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    cancel.assert_awaited_once()
