from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.config import settings
from app.services import trial_channel_service as service


@pytest.mark.parametrize(
    ('status', 'member', 'expected'),
    [
        ('member', False, True),
        ('administrator', False, True),
        ('creator', False, True),
        ('restricted', True, True),
        ('restricted', False, False),
        ('left', False, False),
        ('kicked', False, False),
    ],
)
def test_membership(status, member, expected):
    assert service.is_channel_member(SimpleNamespace(status=status, is_member=member)) == expected


@pytest.mark.asyncio
async def test_trial_channel_required_even_without_global_gate(monkeypatch):
    monkeypatch.setattr(settings, 'CHANNEL_IS_REQUIRED_SUB', False)
    monkeypatch.setattr(settings, 'CHANNEL_TRIAL_REQUIRED', True)
    channels = [{'channel_id': '-1001', 'channel_link': 'https://t.me/test'}]
    monkeypatch.setattr(service.channel_subscription_service, 'get_required_channels', AsyncMock(return_value=channels))
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status='left')))
    with pytest.raises(HTTPException) as error:
        await service.require_trial_channels(SimpleNamespace(telegram_id=123), bot)
    assert error.value.status_code == 403
    bot.get_chat_member.return_value = SimpleNamespace(status='member')
    await service.require_trial_channels(SimpleNamespace(telegram_id=123), bot)


@pytest.mark.asyncio
async def test_api_failure_does_not_issue_trial(monkeypatch):
    monkeypatch.setattr(settings, 'CHANNEL_TRIAL_REQUIRED', True)
    monkeypatch.setattr(
        service.channel_subscription_service, 'get_required_channels', AsyncMock(return_value=[{'channel_id': '-1001'}])
    )
    bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=TimeoutError))
    with pytest.raises(HTTPException) as error:
        await service.require_trial_channels(SimpleNamespace(telegram_id=123), bot)
    assert error.value.status_code == 503


def test_trial_only_policy_never_disables_paid_subscription(monkeypatch):
    from app.services.channel_subscription_service import ChannelSubscriptionService

    monkeypatch.setattr(settings, 'CHANNEL_TRIAL_REQUIRED', True)
    monkeypatch.setattr(settings, 'CHANNEL_IS_REQUIRED_SUB', False)
    monkeypatch.setattr(settings, 'CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE', True)
    channel = {'disable_trial_on_leave': True, 'disable_paid_on_leave': True}
    assert ChannelSubscriptionService.should_disable_subscription(channel, is_trial=True)
    assert not ChannelSubscriptionService.should_disable_subscription(channel, is_trial=False)
