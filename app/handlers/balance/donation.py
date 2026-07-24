"""«Поддержать проект» — пожертвования из бота.

Донат = обычный платёж у тех же платёжных систем, что и пополнение, но с
пометкой ``purpose=donation``: вебхук провайдера НЕ зачисляет сумму на баланс,
а записывает пожертвование (``app/services/payment/common.py::try_fulfill_donation``).
Ровно тот же флоу уже работает в кабинете (кнопка «Поддержать проект» →
``/balance/top-up?purpose=donation``), здесь — его версия для бота.

Флоу: ``menu_donate`` → способ оплаты (``donate_<method>``) → сумма
(``donate_amount|<method>|<kopeks>`` или ввод текстом) → ссылка на оплату.
Благодарность донору и уведомление админам шлёт ``try_fulfill_donation``.
"""

import html

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import get_payment_methods_keyboard
from app.keyboards.topup_amounts import get_topup_amount_keyboard, resolve_config_method_id
from app.localization.texts import get_texts
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

# Tribute — внешняя donate-ссылка без локальной записи платежа, пометить её
# нечем, поэтому в донат-режиме она недоступна (так же, как в кабинете).
EXCLUDED_DONATION_METHODS = frozenset({'tribute'})

# Способы, которые бот называет иначе, чем провайдерный диспетчер платежей
# (PaymentService._create_tagged_payment). Остальные совпадают один-в-один.
_METHOD_ALIASES = {
    'stars': 'telegram_stars',
}

DONATION_MENU_CALLBACK = 'menu_donate'


def _to_provider_method(method: str) -> str:
    """Метод из callback бота → идентификатор для create_donation_payment."""
    if method.startswith('platega_m'):
        # Бот: platega_m2, диспетчер платежей: platega_2
        return f'platega_{method[len("platega_m") :]}'
    return _METHOD_ALIASES.get(method, method)


async def _get_method_limits(db: AsyncSession, method: str) -> tuple[int, int]:
    """Мин/макс суммы выбранного способа (настройка из кабинета, иначе env)."""
    from app.services.payment_method_config_service import _get_method_defaults, get_config_by_method_id

    method_id = resolve_config_method_id(method)
    defaults = _get_method_defaults().get(method_id, {})
    min_amount = defaults.get('default_min', 1000)
    max_amount = defaults.get('default_max', 10000000)

    try:
        config = await get_config_by_method_id(db, method_id)
    except Exception as error:  # pragma: no cover - диагностика, лимиты берём из env
        logger.warning('Не удалось прочитать лимиты способа оплаты', method=method, error=error)
        config = None

    if config is not None:
        if config.min_amount_kopeks is not None:
            min_amount = config.min_amount_kopeks
        if config.max_amount_kopeks is not None:
            max_amount = config.max_amount_kopeks

    return int(min_amount), int(max_amount)


def _donations_available(db_user: User) -> str | None:
    """Причина, по которой донат недоступен, либо None."""
    if not settings.DONATIONS_ENABLED:
        return 'disabled'
    if getattr(db_user, 'restriction_topup', False):
        return 'restricted'
    return None


def _unavailable_message(db_user: User, reason: str) -> str:
    if reason == 'restricted':
        return html.escape(getattr(db_user, 'restriction_reason', None) or 'Действие ограничено администратором')
    return get_texts(db_user.language).t('DONATION_DISABLED', 'Пожертвования сейчас недоступны.')


async def _answer_unavailable(callback: types.CallbackQuery, db_user: User, reason: str) -> None:
    await callback.answer(_unavailable_message(db_user, reason), show_alert=True)


async def _edit_or_answer(callback: types.CallbackQuery, text: str, reply_markup) -> None:
    """Перерисовать экран: у меню бывает и текст, и подпись под картинкой."""
    from aiogram.exceptions import TelegramBadRequest

    if isinstance(callback.message, InaccessibleMessage):
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode='HTML')
        return

    try:
        if callback.message.text:
            await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
        else:
            await callback.message.edit_caption(caption=text, reply_markup=reply_markup, parse_mode='HTML')
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode='HTML')


@error_handler
async def show_donation_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Экран «Поддержать проект» со списком способов пожертвования."""
    texts = get_texts(db_user.language)

    reason = _donations_available(db_user)
    if reason:
        await _answer_unavailable(callback, db_user, reason)
        return

    await state.clear()

    text = texts.t(
        'DONATION_MENU_TEXT',
        '💜 <b>Поддержать проект</b>\n\n'
        'Проект живёт и развивается на средства пользователей: серверы, каналы связи, поддержка.\n\n'
        '⚠️ Это <b>пожертвование</b> — сумма не зачисляется на баланс, а идёт на развитие проекта.\n\n'
        'Выберите способ пожертвования:',
    )

    keyboard = get_payment_methods_keyboard(
        0,
        db_user.language,
        callback_prefix='donate',
        back_callback='back_to_menu',
        include_support_topup=False,
        excluded_methods=EXCLUDED_DONATION_METHODS,
    )

    await _edit_or_answer(callback, text, keyboard)
    await callback.answer()


@error_handler
async def start_donation_amount(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Выбран способ: спрашиваем сумму пожертвования."""
    texts = get_texts(db_user.language)

    reason = _donations_available(db_user)
    if reason:
        await _answer_unavailable(callback, db_user, reason)
        return

    method = callback.data[len('donate_') :]
    if not method:
        await callback.answer('❌ Неизвестный способ оплаты', show_alert=True)
        return

    min_amount, max_amount = await _get_method_limits(db, method)

    text = texts.t(
        'DONATION_AMOUNT_PROMPT',
        '💜 <b>Сумма пожертвования</b>\n\n'
        'Выберите сумму или введите свою (в рублях) — от {min_amount} до {max_amount}.\n\n'
        '⚠️ Сумма не зачисляется на баланс — это поддержка проекта.',
    ).format(
        min_amount=texts.format_price(min_amount),
        max_amount=texts.format_price(max_amount),
    )

    keyboard = await get_topup_amount_keyboard(
        method,
        db_user.language,
        db,
        back_callback=DONATION_MENU_CALLBACK,
        min_amount_kopeks=min_amount,
        callback_prefix='donate_amount',
    )

    await _edit_or_answer(callback, text, keyboard)

    await state.set_state(BalanceStates.waiting_for_donation_amount)
    await state.update_data(
        donation_method=method,
        donation_prompt_message_id=callback.message.message_id,
        donation_prompt_chat_id=callback.message.chat.id,
    )
    await callback.answer()


@error_handler
async def handle_donation_amount_callback(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Быстрая сумма пожертвования кнопкой."""
    reason = _donations_available(db_user)
    if reason:
        await _answer_unavailable(callback, db_user, reason)
        return

    try:
        _, method, amount_str = callback.data.split('|', 2)
        amount_kopeks = int(amount_str)
    except ValueError:
        await callback.answer('❌ Некорректный запрос', show_alert=True)
        return

    await callback.answer()
    await _create_donation_and_respond(
        callback.message,
        db_user,
        db,
        state,
        method=method,
        amount_kopeks=amount_kopeks,
        edit_message=not isinstance(callback.message, InaccessibleMessage),
    )


@error_handler
async def process_donation_amount(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Сумма пожертвования, введённая текстом."""
    texts = get_texts(db_user.language)

    reason = _donations_available(db_user)
    if reason:
        await state.clear()
        await message.answer(_unavailable_message(db_user, reason))
        return

    data = await state.get_data()
    method = data.get('donation_method')
    if not method:
        await state.clear()
        await message.answer(texts.t('DONATION_DISABLED', 'Пожертвования сейчас недоступны.'))
        return

    raw_amount = (message.text or '').strip()
    try:
        amount_rubles = float(raw_amount.replace(',', '.'))
    except ValueError:
        await message.answer(texts.INVALID_AMOUNT)
        return

    amount_kopeks = int(round(amount_rubles * 100))
    if amount_kopeks <= 0:
        await message.answer(texts.INVALID_AMOUNT)
        return

    await _create_donation_and_respond(
        message,
        db_user,
        db,
        state,
        method=method,
        amount_kopeks=amount_kopeks,
        edit_message=False,
    )


async def _create_donation_and_respond(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    *,
    method: str,
    amount_kopeks: int,
    edit_message: bool,
) -> None:
    """Создать донат-платёж и показать кнопку оплаты."""
    from app.services.payment_service import PaymentService

    texts = get_texts(db_user.language)
    min_amount, max_amount = await _get_method_limits(db, method)

    back_keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data=DONATION_MENU_CALLBACK)]]
    )

    async def _fail(text: str) -> None:
        if edit_message:
            await message.edit_text(text, reply_markup=back_keyboard, parse_mode='HTML')
        else:
            await message.answer(text, reply_markup=back_keyboard, parse_mode='HTML')

    if amount_kopeks < min_amount:
        await _fail(
            texts.t('DONATION_AMOUNT_TOO_SMALL', '❌ Минимальная сумма для этого способа: {amount}').format(
                amount=texts.format_price(min_amount)
            )
        )
        return

    if amount_kopeks > max_amount:
        await _fail(
            texts.t('DONATION_AMOUNT_TOO_BIG', '❌ Максимальная сумма для этого способа: {amount}').format(
                amount=texts.format_price(max_amount)
            )
        )
        return

    provider_method = _to_provider_method(method)
    stars_amount: int | None = None

    if provider_method == 'telegram_stars':
        # Округляем до целого числа звёзд: записанное пожертвование считается из
        # звёзд, и без нормализации показанная сумма разошлась бы с фактической.
        try:
            stars_amount = settings.rubles_to_stars(amount_kopeks / 100)
            amount_kopeks = round(stars_amount * settings.get_stars_rate() * 100)
        except ValueError:
            await _fail(texts.t('DONATION_CREATE_ERROR', '❌ Не удалось создать платёж. Попробуйте позже.'))
            return

    bot_username = settings.get_bot_username()
    return_url = f'https://t.me/{bot_username}' if bot_username else ''

    payment_service = PaymentService(message.bot)
    try:
        result = await payment_service.create_donation_payment(
            db,
            user_id=db_user.id,
            amount_kopeks=amount_kopeks,
            payment_method=provider_method,
            description=settings.get_custom_payment_description('Пожертвование на развитие проекта'),
            return_url=return_url,
        )
    except Exception as error:
        logger.error('Ошибка создания донат-платежа', method=provider_method, error=error, exc_info=True)
        result = None

    payment_url = (result or {}).get('payment_url')
    if not payment_url:
        logger.error('Донат-платёж не создан', method=provider_method, user_id=db_user.id)
        await _fail(texts.t('DONATION_CREATE_ERROR', '❌ Не удалось создать платёж. Попробуйте позже.'))
        await state.clear()
        return

    lines = [
        texts.t('DONATION_PAY_TITLE', '💜 <b>Поддержка проекта</b>'),
        '',
        # round_kopeks=False: у Stars сумма подгоняется под целое число звёзд и
        # может быть с копейками — показываем ровно то, что запишется в донаты.
        texts.t('DONATION_PAY_AMOUNT', '💰 Сумма: {amount}').format(
            amount=texts.format_price(amount_kopeks, round_kopeks=False)
        ),
    ]
    if stars_amount:
        lines.append(
            texts.t('DONATION_PAY_STARS', '⭐ К оплате: {stars} звёзд').format(stars=stars_amount),
        )
    lines.extend(
        [
            '',
            texts.t(
                'DONATION_PAY_HINT',
                'Нажмите кнопку ниже, чтобы завершить платёж.\n'
                'Сумма не зачисляется на баланс — это ваш вклад в развитие проекта. Спасибо! 🙏',
            ),
        ]
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('DONATION_PAY_BUTTON', '💜 Поддержать'),
                    url=payment_url,
                )
            ],
            [types.InlineKeyboardButton(text=texts.BACK, callback_data=DONATION_MENU_CALLBACK)],
        ]
    )

    text = '\n'.join(lines)
    if edit_message:
        await message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    else:
        # Сумма пришла текстом: убираем запрос суммы и само сообщение с числом,
        # чтобы в чате остался только экран оплаты.
        state_data = await state.get_data()
        prompt_message_id = state_data.get('donation_prompt_message_id')
        prompt_chat_id = state_data.get('donation_prompt_chat_id', message.chat.id)
        for chat_id, message_id in ((message.chat.id, message.message_id), (prompt_chat_id, prompt_message_id)):
            if not message_id:
                continue
            try:
                await message.bot.delete_message(chat_id, message_id)
            except Exception as delete_error:  # pragma: no cover - зависит от прав бота
                logger.warning('Не удалось удалить сообщение донат-флоу', error=delete_error)
        await message.answer(text, reply_markup=keyboard, parse_mode='HTML')

    await state.clear()

    logger.info(
        '💜 Создан донат-платёж',
        user_id=db_user.id,
        method=provider_method,
        amount_kopeks=amount_kopeks,
    )


def register_donation_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_donation_menu, F.data == DONATION_MENU_CALLBACK)
    dp.callback_query.register(handle_donation_amount_callback, F.data.startswith('donate_amount|'))
    dp.callback_query.register(
        start_donation_amount,
        F.data.startswith('donate_') & ~F.data.startswith('donate_amount|'),
    )
    # ~F.successful_payment: сообщение об оплате Stars не должно трактоваться как
    # ввод суммы (см. тот же приём для обычного пополнения в main.py).
    dp.message.register(process_donation_amount, BalanceStates.waiting_for_donation_amount, ~F.successful_payment)
