"""Экран донат-апселла в боте: что показывает и с какой суммой продолжает оплату."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.handlers.balance.donation_upsell as upsell_mod
from app.config import settings
from app.services.donation_upsell_service import UpsellOffer


def _user():
    user = MagicMock()
    user.id = 42
    user.language = 'ru'
    user.telegram_id = 111
    user.restriction_topup = False
    return user


def _callback(data: str = 'dupsell_yes'):
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.text = 'экран суммы'
    callback.message.chat.id = 111
    callback.message.message_id = 5
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


def _state(data: dict | None = None):
    state = AsyncMock()
    state.get_data = AsyncMock(return_value=data or {})
    return state


def _offer(tip: int = 1000, topup: int = 50000, presets=(2500, 5000)):
    return UpsellOffer(
        tip_kopeks=tip,
        presets_kopeks=list(presets),
        topup_amount_kopeks=topup,
        total_kopeks=topup + tip,
        max_tip_kopeks=5_000_000,
    )


@pytest.mark.asyncio
async def test_screen_shown_with_choices_and_back(monkeypatch):
    monkeypatch.setattr(upsell_mod, 'build_offer', AsyncMock(return_value=_offer()))
    callback = _callback()

    shown = await upsell_mod.maybe_show_upsell(
        callback, _user(), MagicMock(), _state(), amount_kopeks=50000, method='yookassa'
    )

    assert shown is True
    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert 'dupsell_yes' in callbacks
    assert 'dupsell_no' in callbacks
    assert 'dupsell_custom' in callbacks
    # «Назад» возвращает на экран выбора суммы того же способа оплаты.
    assert 'topup_yookassa' in callbacks
    text = callback.message.edit_text.call_args.args[0]
    assert '510' in text  # итоговая сумма: 500 ₽ пополнение + 10 ₽ поддержка


@pytest.mark.asyncio
async def test_back_button_targets_submethod_amount_screen(monkeypatch):
    """Подметоды (СБП/карта, platega_m<N>) — свой экран сумм, свой колбэк возврата."""
    monkeypatch.setattr(upsell_mod, 'build_offer', AsyncMock(return_value=_offer()))
    callback = _callback()

    await upsell_mod.maybe_show_upsell(
        callback, _user(), MagicMock(), _state(), amount_kopeks=50000, method='platega_m2'
    )

    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    assert markup.inline_keyboard[-1][0].callback_data == 'topup_platega_m2'


@pytest.mark.asyncio
async def test_no_screen_when_offer_is_none(monkeypatch):
    monkeypatch.setattr(upsell_mod, 'build_offer', AsyncMock(return_value=None))
    callback = _callback()

    shown = await upsell_mod.maybe_show_upsell(
        callback, _user(), MagicMock(), _state(), amount_kopeks=50000, method='tribute'
    )

    assert shown is False
    callback.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_accept_registers_pledge_and_dispatches_total(monkeypatch):
    register_pledge = AsyncMock(return_value=MagicMock(id=3))
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(upsell_mod, 'register_pledge', register_pledge)
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', dispatch)

    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})
    await upsell_mod.handle_upsell_accept(_callback('dupsell_yes'), _user(), MagicMock(), state)

    assert register_pledge.await_args.kwargs['tip_kopeks'] == 1000
    assert register_pledge.await_args.kwargs['source'] == 'bot'
    assert dispatch.await_args.kwargs['amount_kopeks'] == 51000
    assert dispatch.await_args.kwargs['method'] == 'yookassa'


@pytest.mark.asyncio
async def test_decline_dispatches_original_amount(monkeypatch):
    register_decline = AsyncMock()
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(upsell_mod, 'register_decline', register_decline)
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', dispatch)

    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})
    await upsell_mod.handle_upsell_decline(_callback('dupsell_no'), _user(), MagicMock(), state)

    register_decline.assert_awaited_once()
    assert dispatch.await_args.kwargs['amount_kopeks'] == 50000


@pytest.mark.asyncio
async def test_expired_session_does_not_dispatch(monkeypatch):
    """Кнопку нажали после перезапуска бота — платить «наугад» нельзя."""
    dispatch = AsyncMock()
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', dispatch)
    callback = _callback('dupsell_yes')

    await upsell_mod.handle_upsell_accept(callback, _user(), MagicMock(), _state({}))

    dispatch.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get('show_alert') is True


@pytest.mark.asyncio
async def test_custom_screen_offers_presets(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_PRESETS', '25,50', raising=False)
    callback = _callback('dupsell_custom')
    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})

    await upsell_mod.handle_upsell_custom(callback, _user(), MagicMock(), state)

    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert 'dupsell_amt|2500' in callbacks
    assert 'dupsell_amt|5000' in callbacks
    assert 'dupsell_back' in callbacks


@pytest.mark.asyncio
async def test_custom_preset_dispatches_sum_with_chosen_tip(monkeypatch):
    monkeypatch.setattr(upsell_mod, 'register_pledge', AsyncMock(return_value=MagicMock(id=4)))
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', dispatch)

    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})
    await upsell_mod.handle_upsell_custom_amount(_callback('dupsell_amt|10000'), _user(), MagicMock(), state)

    assert dispatch.await_args.kwargs['amount_kopeks'] == 60000


@pytest.mark.asyncio
async def test_typed_tip_rejected_when_over_method_limit(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(upsell_mod, '_route_payment', dispatch)
    monkeypatch.setattr(upsell_mod, 'resolve_max_tip_kopeks', MagicMock(return_value=5000))

    message = MagicMock()
    message.text = '1000'
    message.answer = AsyncMock()
    state = _state(
        {
            'upsell_topup_amount': 50000,
            'upsell_tip': 1000,
            'upsell_method': 'yookassa',
            'upsell_method_max': 55000,
        }
    )

    await upsell_mod.process_upsell_custom_amount(message, _user(), MagicMock(), state)

    dispatch.assert_not_awaited()
    message.answer.assert_awaited()


@pytest.mark.asyncio
async def test_typed_tip_dispatches_total(monkeypatch):
    monkeypatch.setattr(upsell_mod, 'register_pledge', AsyncMock(return_value=MagicMock(id=5)))
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(upsell_mod, '_route_payment', dispatch)

    message = MagicMock()
    message.text = '50'
    message.answer = AsyncMock()
    message.chat.id = 111
    message.message_id = 9
    message.bot = MagicMock()
    message.bot.delete_message = AsyncMock()
    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})

    await upsell_mod.process_upsell_custom_amount(message, _user(), MagicMock(), state)

    assert dispatch.await_args.kwargs['amount_kopeks'] == 55000


# --- Экрана нет — прежнее обещание гасим -----------------------------------------


@pytest.mark.asyncio
async def test_stale_pledge_is_cancelled_when_screen_not_shown(monkeypatch):
    """Cooldown/порог/Tribute: экрана нет, но пополнение начато — старое обещание не наше."""
    cancel = AsyncMock(return_value=1)
    monkeypatch.setattr(upsell_mod, 'build_offer', AsyncMock(return_value=None))
    monkeypatch.setattr(upsell_mod, 'cancel_pending_pledges', cancel)
    db = MagicMock()
    user = _user()

    shown = await upsell_mod.maybe_show_upsell(_callback(), user, db, _state(), amount_kopeks=50000, method='tribute')

    assert shown is False
    cancel.assert_awaited_once_with(db, user)


@pytest.mark.asyncio
async def test_shown_screen_does_not_cancel_pledges(monkeypatch):
    """Экран показан — решение придёт кнопкой, оно само погасит прежнее обещание."""
    cancel = AsyncMock()
    monkeypatch.setattr(upsell_mod, 'build_offer', AsyncMock(return_value=_offer()))
    monkeypatch.setattr(upsell_mod, 'cancel_pending_pledges', cancel)

    await upsell_mod.maybe_show_upsell(
        _callback(), _user(), MagicMock(), _state(), amount_kopeks=50000, method='yookassa'
    )

    cancel.assert_not_awaited()


# --- Расшифровка счёта после согласия ---------------------------------------------


@pytest.mark.asyncio
async def test_payment_note_explains_invoice_after_accept(monkeypatch):
    """Провайдер покажет только итог — из чего он состоит, человек узнаёт от нас."""
    monkeypatch.setattr(upsell_mod, 'register_pledge', AsyncMock(return_value=MagicMock(id=3)))
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', AsyncMock(return_value=True))
    callback = _callback('dupsell_yes')

    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})
    await upsell_mod.handle_upsell_accept(callback, _user(), MagicMock(), state)

    note = callback.message.answer.await_args.args[0]
    assert '510' in note  # к оплате
    assert '500' in note  # на баланс
    assert '10' in note  # поддержка проекта


@pytest.mark.asyncio
async def test_payment_note_sent_on_typed_tip(monkeypatch):
    """Ветка «сумма текстом» чистит чат — пояснение уходит отдельным сообщением после."""
    monkeypatch.setattr(upsell_mod, 'register_pledge', AsyncMock(return_value=MagicMock(id=5)))
    monkeypatch.setattr(upsell_mod, '_route_payment', AsyncMock(return_value=True))

    message = MagicMock()
    message.text = '50'
    message.answer = AsyncMock()
    message.chat.id = 111
    message.message_id = 9
    message.bot = MagicMock()
    message.bot.delete_message = AsyncMock()
    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})

    await upsell_mod.process_upsell_custom_amount(message, _user(), MagicMock(), state)

    note = message.answer.await_args.args[0]
    assert '550' in note  # 500 ₽ пополнение + 50 ₽ поддержки
    assert '50' in note


@pytest.mark.asyncio
async def test_no_payment_note_without_tip(monkeypatch):
    """Отказ — обычное пополнение, никаких пояснений про поддержку."""
    monkeypatch.setattr(upsell_mod, 'register_decline', AsyncMock())
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', AsyncMock(return_value=True))
    callback = _callback('dupsell_no')

    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})
    await upsell_mod.handle_upsell_decline(callback, _user(), MagicMock(), state)

    callback.message.answer.assert_not_called()


# --- «Назад» возвращает туда, откуда пришли ---------------------------------------


@pytest.mark.asyncio
async def test_back_returns_to_cart_screen_when_amount_was_prefilled(monkeypatch):
    """Корзина подставила сумму: сетки сумм не было, возвращаем на способы оплаты."""
    monkeypatch.setattr(upsell_mod, 'build_offer', AsyncMock(return_value=_offer()))
    callback = _callback()
    state = _state({'upsell_back_callback': 'balance_topup', 'upsell_back_amount': 50000})

    await upsell_mod.maybe_show_upsell(callback, _user(), MagicMock(), state, amount_kopeks=50000, method='yookassa')

    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    assert markup.inline_keyboard[-1][0].callback_data == 'balance_topup'


@pytest.mark.asyncio
async def test_back_returns_to_tariff_when_shortfall_started_the_flow(monkeypatch):
    """«Не хватает на тариф» — возврат к тарифу, а не в общее пополнение."""
    monkeypatch.setattr(upsell_mod, 'build_offer', AsyncMock(return_value=_offer(topup=30000)))
    callback = _callback()
    state = _state({'upsell_back_callback': 'tariff_select:7', 'upsell_back_amount': 30000})

    await upsell_mod.maybe_show_upsell(callback, _user(), MagicMock(), state, amount_kopeks=30000, method='platega_m2')

    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    assert markup.inline_keyboard[-1][0].callback_data == 'tariff_select:7'


@pytest.mark.asyncio
async def test_stale_back_hint_is_ignored(monkeypatch):
    """Подсказка живёт в state дольше своего сценария — сверяем её с суммой."""
    monkeypatch.setattr(upsell_mod, 'build_offer', AsyncMock(return_value=_offer()))
    callback = _callback()
    state = _state({'upsell_back_callback': 'tariff_select:7', 'upsell_back_amount': 30000})

    await upsell_mod.maybe_show_upsell(callback, _user(), MagicMock(), state, amount_kopeks=50000, method='yookassa')

    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    assert markup.inline_keyboard[-1][0].callback_data == 'topup_yookassa'


@pytest.mark.asyncio
async def test_back_from_custom_screen_reuses_resolved_target(monkeypatch):
    """Возврат с экрана «иная сумма» ведёт туда же, куда и с основного."""
    callback = _callback('dupsell_back')
    state = _state(
        {
            'upsell_topup_amount': 30000,
            'upsell_tip': 1000,
            'upsell_method': 'platega_m2',
            'upsell_back': 'tariff_select:7',
        }
    )

    await upsell_mod.handle_upsell_back(callback, _user(), MagicMock(), state)

    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    assert markup.inline_keyboard[-1][0].callback_data == 'tariff_select:7'


@pytest.mark.asyncio
async def test_click_on_stale_screen_is_rejected(monkeypatch):
    """Старый экран апселта в истории чата не должен оплачивать сумму нового."""
    dispatch = AsyncMock()
    register_pledge = AsyncMock()
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', dispatch)
    monkeypatch.setattr(upsell_mod, 'register_pledge', register_pledge)

    callback = _callback('dupsell_yes')
    callback.message.message_id = 5
    state = _state(
        {
            'upsell_topup_amount': 100000,
            'upsell_tip': 1000,
            'upsell_method': 'yookassa',
            'upsell_prompt_message_id': 77,  # актуален другой экран
        }
    )

    await upsell_mod.handle_upsell_accept(callback, _user(), MagicMock(), state)

    dispatch.assert_not_awaited()
    register_pledge.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get('show_alert') is True


@pytest.mark.asyncio
async def test_click_on_current_screen_is_accepted(monkeypatch):
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', dispatch)
    monkeypatch.setattr(upsell_mod, 'register_pledge', AsyncMock(return_value=MagicMock(id=1)))

    callback = _callback('dupsell_yes')
    callback.message.message_id = 77
    state = _state(
        {
            'upsell_topup_amount': 50000,
            'upsell_tip': 1000,
            'upsell_method': 'yookassa',
            'upsell_prompt_message_id': 77,
        }
    )

    await upsell_mod.handle_upsell_accept(callback, _user(), MagicMock(), state)

    assert dispatch.await_args.kwargs['amount_kopeks'] == 51000


@pytest.mark.asyncio
async def test_context_without_known_prompt_id_still_works(monkeypatch):
    """Если id экрана не сохранился (отправка новым сообщением) — сверять нечего."""
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', dispatch)
    monkeypatch.setattr(upsell_mod, 'register_pledge', AsyncMock(return_value=MagicMock(id=1)))

    callback = _callback('dupsell_yes')
    callback.message.message_id = 5
    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})

    await upsell_mod.handle_upsell_accept(callback, _user(), MagicMock(), state)

    dispatch.assert_awaited_once()
