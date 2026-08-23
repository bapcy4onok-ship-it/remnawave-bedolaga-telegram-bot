"""История баланса: надбавка уменьшила баланс — показываем «−», обычный донат — нет."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.balance.main as balance_main
from app.database.models import TransactionType


def _transaction(external_id: str | None, amount_kopeks: int = 1000):
    transaction = MagicMock()
    transaction.type = TransactionType.DONATION.value
    transaction.amount_kopeks = amount_kopeks
    transaction.external_id = external_id
    transaction.description = 'Пожертвование'
    transaction.created_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    return transaction


def _callback():
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.text = 'история'
    callback.message.edit_text = AsyncMock()
    return callback


def _user():
    user = MagicMock()
    user.id = 42
    user.language = 'ru'
    return user


async def _render(transaction):
    callback = _callback()
    with patch.object(balance_main, 'get_user_transactions', AsyncMock(return_value=[transaction])):
        await balance_main.show_balance_history(callback, _user(), MagicMock())
    return callback.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_upsell_donation_rendered_as_debit():
    text = await _render(_transaction('pledge:7'))

    assert '❤️ -' in text


@pytest.mark.asyncio
async def test_menu_donation_rendered_without_sign():
    text = await _render(_transaction('yookassa-payment-id'))

    assert '❤️ -' not in text
    assert '❤️ ' in text
