"""Пост-хук пополнения через Tribute не зависит от доставки уведомления.

За пост-хуком стоит списание надбавки донат-апселла и возврат к сохранённой корзине.
Реферальная комиссия считается раньше и уже вычитает надбавку, поэтому упавший
``send_message`` (человек заблокировал бота) не имеет права унести с собой списание.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.services.payment.common as payment_common
import app.services.tribute_service as tribute_module


@pytest.fixture
def tribute_env(monkeypatch):
    user = SimpleNamespace(id=42, telegram_id=111, balance_kopeks=51000)

    async def fake_get_db():
        yield MagicMock()

    monkeypatch.setattr(tribute_module, 'get_db', fake_get_db)
    monkeypatch.setattr(tribute_module, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(
        tribute_module,
        'PaymentService',
        MagicMock(return_value=SimpleNamespace(build_topup_success_keyboard=AsyncMock(return_value=None))),
    )

    post_hook = AsyncMock()
    monkeypatch.setattr(payment_common, 'send_cart_notification_after_topup', post_hook)

    return SimpleNamespace(user=user, post_hook=post_hook)


async def test_post_hook_runs_after_successful_notification(tribute_env):
    bot = SimpleNamespace(send_message=AsyncMock())
    service = tribute_module.TributeService(bot)

    await service._send_success_notification(111, 51000)

    bot.send_message.assert_awaited_once()
    tribute_env.post_hook.assert_awaited_once()
    assert tribute_env.post_hook.await_args.args[1] == 51000


async def test_post_hook_runs_even_if_notification_fails(tribute_env):
    """Бот заблокирован — надбавку всё равно надо списать, корзину предложить."""
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('bot was blocked by the user')))
    service = tribute_module.TributeService(bot)

    await service._send_success_notification(111, 51000)

    tribute_env.post_hook.assert_awaited_once()
