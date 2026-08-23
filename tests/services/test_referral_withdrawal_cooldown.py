"""Cooldown на вывод реф. баланса отсчитывается только от успешных выводов."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.database.models import WithdrawalRequestStatus
from app.services.referral_withdrawal_service import ReferralWithdrawalService


STATS = {
    'total_earned': 1_500_000,
    'own_deposits': 5_000,
    'spending': 300_000,
    'referral_spent': 300_000,
    'withdrawn': 0,
    'pending': 0,
    'available_referral': 1_200_000,
    'available_total': 1_200_000,
    'only_referral_mode': True,
}


@pytest.fixture
def enabled_settings(monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_ENABLED', True)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_PARTNERS_ONLY', False)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_TEST_MODE', False)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_COOLDOWN_DAYS', 30)
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS', 50_000)


def _service(*, pending: bool = False, last_success=None) -> ReferralWithdrawalService:
    service = ReferralWithdrawalService()
    service.get_referral_balance_stats = AsyncMock(return_value=dict(STATS))
    service.has_pending_withdrawal_request = AsyncMock(return_value=pending)
    service.get_last_successful_withdrawal = AsyncMock(return_value=last_success)
    return service


def _request(status: str, *, created_days_ago: int, processed_days_ago: int | None = None):
    request = MagicMock()
    request.status = status
    request.created_at = datetime.now(UTC) - timedelta(days=created_days_ago)
    request.processed_at = (
        None if processed_days_ago is None else datetime.now(UTC) - timedelta(days=processed_days_ago)
    )
    return request


@pytest.mark.asyncio
async def test_cancelled_request_does_not_burn_cooldown(enabled_settings):
    """Партнёр отменил свою же заявку — повторную подачу это блокировать не должно."""
    service = _service(last_success=None)

    can_request, reason, _ = await service.can_request_withdrawal(AsyncMock(), user_id=1)

    assert can_request is True
    assert reason == 'OK'


@pytest.mark.asyncio
async def test_recent_successful_withdrawal_blocks(enabled_settings):
    service = _service(last_success=_request(WithdrawalRequestStatus.COMPLETED.value, created_days_ago=5, processed_days_ago=4))

    can_request, reason, _ = await service.can_request_withdrawal(AsyncMock(), user_id=1)

    assert can_request is False
    assert 'через 26 дн.' in reason


@pytest.mark.asyncio
async def test_old_successful_withdrawal_does_not_block(enabled_settings):
    service = _service(last_success=_request(WithdrawalRequestStatus.APPROVED.value, created_days_ago=40, processed_days_ago=39))

    can_request, reason, _ = await service.can_request_withdrawal(AsyncMock(), user_id=1)

    assert can_request is True
    assert reason == 'OK'


@pytest.mark.asyncio
async def test_cooldown_falls_back_to_created_at(enabled_settings):
    """Старые заявки без processed_at — считаем от даты подачи."""
    service = _service(last_success=_request(WithdrawalRequestStatus.APPROVED.value, created_days_ago=2))

    can_request, reason, _ = await service.can_request_withdrawal(AsyncMock(), user_id=1)

    assert can_request is False
    assert 'через 28 дн.' in reason


@pytest.mark.asyncio
async def test_pending_request_still_blocks(enabled_settings):
    service = _service(pending=True)

    can_request, reason, _ = await service.can_request_withdrawal(AsyncMock(), user_id=1)

    assert can_request is False
    assert reason == 'У вас уже есть активная заявка на рассмотрении'


@pytest.mark.asyncio
async def test_test_mode_skips_cooldown(enabled_settings, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_WITHDRAWAL_TEST_MODE', True)
    service = _service(last_success=_request(WithdrawalRequestStatus.COMPLETED.value, created_days_ago=1, processed_days_ago=1))

    can_request, reason, _ = await service.can_request_withdrawal(AsyncMock(), user_id=1)

    assert can_request is True
    assert reason == 'OK'
