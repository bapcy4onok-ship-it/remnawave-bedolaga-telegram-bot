"""Донат-флоу бота: «Поддержать проект» не должен трогать баланс.

Прогоняем реальные хендлеры (тяжёлый I/O замокан) и проверяем то, из-за чего
пожертвование может незаметно превратиться в пополнение:
- у всех кнопок способов оплаты префикс `donate_` (иначе попадём в топап-ветку);
- Tribute (внешняя donate-ссылка без локальной записи) в списке отсутствует;
- сумма уходит в create_donation_payment с id донора и выбранным методом;
- Stars нормализуются под целое число звёзд;
- сумма ниже минимума способа платёж не создаёт;
- DONATIONS_ENABLED=false закрывает вход.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.balance.donation as donation_mod
from app.config import settings
from app.keyboards.inline import get_main_menu_keyboard


def _make_user():
    user = MagicMock()
    user.id = 42
    user.language = 'ru'
    user.telegram_id = 111
    user.restriction_topup = False
    return user


def _make_message(text: str = '500'):
    message = MagicMock()
    message.text = text
    message.chat.id = 111
    message.message_id = 5
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.bot = MagicMock()
    message.bot.delete_message = AsyncMock()
    return message


def _make_callback(data: str):
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.text = 'previous screen'
    callback.message.chat.id = 111
    callback.message.message_id = 5
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


def _state(data: dict | None = None):
    state = AsyncMock()
    state.get_data = AsyncMock(return_value=data or {})
    return state


class _CapturingPaymentService:
    """Подменяет PaymentService: запоминает вызов вместо похода к провайдеру."""

    calls: list[dict] = []

    def __init__(self, bot=None):
        pass

    async def create_donation_payment(self, db, **kwargs):
        type(self).calls.append(kwargs)
        return {'payment_url': 'https://pay.example/abc', 'provider': 'test'}


def _patched_service(min_kopeks: int = 5000, max_kopeks: int = 10_000_000):
    _CapturingPaymentService.calls = []
    return (
        patch('app.services.payment_service.PaymentService', _CapturingPaymentService),
        patch.object(donation_mod, '_get_method_limits', AsyncMock(return_value=(min_kopeks, max_kopeks))),
    )


@pytest.mark.asyncio
async def test_donation_menu_offers_only_donation_callbacks(monkeypatch):
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'TRIBUTE_ENABLED', True, raising=False)

    callback = _make_callback('menu_donate')
    await donation_mod.show_donation_menu(callback, _make_user(), MagicMock(), _state())

    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert callbacks, 'экран доната без кнопок'
    assert all(data.startswith('donate_') or data == 'back_to_menu' for data in callbacks), callbacks
    assert 'donate_tribute' not in callbacks
    assert 'topup_support' not in callbacks


@pytest.mark.asyncio
async def test_typed_amount_creates_donation_for_donor(monkeypatch):
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    service_patch, limits_patch = _patched_service()

    message = _make_message('500')
    with service_patch, limits_patch:
        await donation_mod.process_donation_amount(
            message, _make_user(), MagicMock(), _state({'donation_method': 'rollypay'})
        )

    assert len(_CapturingPaymentService.calls) == 1
    call = _CapturingPaymentService.calls[0]
    assert call['user_id'] == 42
    assert call['amount_kopeks'] == 50000
    assert call['payment_method'] == 'rollypay'

    markup = message.answer.call_args.kwargs['reply_markup']
    assert markup.inline_keyboard[0][0].url == 'https://pay.example/abc'


@pytest.mark.asyncio
async def test_amount_below_method_minimum_creates_nothing(monkeypatch):
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    service_patch, limits_patch = _patched_service(min_kopeks=5000)

    message = _make_message('10')
    with service_patch, limits_patch:
        await donation_mod.process_donation_amount(
            message, _make_user(), MagicMock(), _state({'donation_method': 'rollypay'})
        )

    assert _CapturingPaymentService.calls == []
    assert 'Минимальная сумма' in message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_stars_amount_is_normalised_to_whole_stars(monkeypatch):
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    service_patch, limits_patch = _patched_service(min_kopeks=100)

    message = _make_message('100')
    with service_patch, limits_patch:
        await donation_mod.process_donation_amount(
            message, _make_user(), MagicMock(), _state({'donation_method': 'stars'})
        )

    call = _CapturingPaymentService.calls[0]
    stars = settings.rubles_to_stars(100)
    assert call['payment_method'] == 'telegram_stars'
    # Записанная сумма считается из звёзд — показанная должна совпадать с ней.
    assert call['amount_kopeks'] == round(stars * settings.get_stars_rate() * 100)


@pytest.mark.asyncio
async def test_quick_amount_button_creates_donation(monkeypatch):
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    service_patch, limits_patch = _patched_service()

    callback = _make_callback('donate_amount|rollypay|100000')
    with service_patch, limits_patch:
        await donation_mod.handle_donation_amount_callback(callback, _make_user(), MagicMock(), _state())

    call = _CapturingPaymentService.calls[0]
    assert call['amount_kopeks'] == 100000
    assert call['payment_method'] == 'rollypay'


@pytest.mark.asyncio
async def test_disabled_donations_block_entry_and_main_menu_button(monkeypatch):
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', False, raising=False)

    callback = _make_callback('menu_donate')
    await donation_mod.show_donation_menu(callback, _make_user(), MagicMock(), _state())

    assert callback.message.edit_text.call_args is None
    assert callback.answer.call_args.kwargs.get('show_alert') is True

    menu = get_main_menu_keyboard(language='ru', balance_kopeks=0)
    assert 'menu_donate' not in [b.callback_data for row in menu.inline_keyboard for b in row]

    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    menu = get_main_menu_keyboard(language='ru', balance_kopeks=0)
    assert menu.inline_keyboard[-1][0].callback_data == 'menu_donate'
