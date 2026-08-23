"""Донат-апселл в боте: «Поддержать проект и добавить 10 ₽?».

Экран вклинивается между выбором суммы пополнения и переходом к оплате. Согласие
увеличивает сумму платежа и создаёт обещание надбавки, отказ — просто отметку.
Дальше флоу продолжает та же разводка, что и обычное пополнение
(``app/handlers/balance/main.py::dispatch_topup``), поэтому провайдерные хендлеры
о надбавке ничего не знают.

Исполняет обещание вебхук: ``app/services/donation_upsell_service.py``.
"""

import math

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.donation_upsell_service import (
    build_offer,
    cancel_pending_pledges,
    register_decline,
    register_pledge,
    resolve_max_tip_kopeks,
)
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

UPSELL_ACCEPT = 'dupsell_yes'
UPSELL_DECLINE = 'dupsell_no'
UPSELL_CUSTOM = 'dupsell_custom'
UPSELL_CUSTOM_PREFIX = 'dupsell_amt|'
UPSELL_BACK = 'dupsell_back'

PLEDGE_SOURCE = 'bot'


async def _method_max_kopeks(db: AsyncSession | None, method: str) -> int | None:
    """Максимум выбранного способа оплаты (настройка кабинета), либо None."""
    if db is None:
        return None
    try:
        from app.keyboards.topup_amounts import resolve_config_method_id
        from app.services.payment_method_config_service import _get_method_defaults, get_config_by_method_id

        method_id = resolve_config_method_id(method)
        defaults = _get_method_defaults().get(method_id, {})
        max_amount = defaults.get('default_max')
        config = await get_config_by_method_id(db, method_id)
        if config is not None and config.max_amount_kopeks is not None:
            max_amount = config.max_amount_kopeks
        return int(max_amount) if max_amount else None
    except Exception as error:  # pragma: no cover — лимиты не должны ломать оплату
        logger.warning('Не удалось получить лимит способа оплаты для апселла', method=method, error=error)
        return None


def _screen_text(texts, *, topup_kopeks: int, tip_kopeks: int) -> str:
    return texts.t(
        'DONATION_UPSELL_PROMPT',
        '<b>Поддержать проект и добавить {tip}?</b> <tg-emoji emoji-id="5192879906295397710">💝</tg-emoji>\n\n'
        '<tg-emoji emoji-id="6244595016287654616">⚠️</tg-emoji> Не зачисляются на баланс, '
        'а идут на увеличение мощностей и скорости соединения.\n\n'
        'Пополнение: <b>{topup}</b>\n'
        'Поддержка: <b>+{tip}</b>\n'
        'К оплате: <b>{total}</b>',
    ).format(
        tip=texts.format_price(tip_kopeks),
        topup=texts.format_price(topup_kopeks),
        total=texts.format_price(topup_kopeks + tip_kopeks),
    )


def _amount_screen_callback(method: str) -> str:
    """Колбэк экрана выбора суммы для способа оплаты.

    Все входы вида ``topup_<method>`` зарегистрированы в
    ``app/handlers/balance/main.py::register_balance_handlers`` (включая подметоды
    СБП/карта и ``topup_platega_m<N>``), так что мёртвой кнопка не будет никогда.
    Это фолбэк: экран сумм и правда предшествует апселлу, если пользователь пришёл
    обычным путём «способ → сумма».
    """
    return f'topup_{method}'


async def _resolve_back_callback(state: FSMContext, *, method: str, amount_kopeks: int) -> str:
    """Куда возвращает «Назад»: на предыдущий экран, а не всегда на сетку сумм.

    Сумма может быть известна ДО выбора способа — «не хватает на тариф», возврат к
    сохранённой корзине. Тогда клавиатуру рисует ``get_payment_methods_keyboard``
    (app/keyboards/inline.py), выдавая тот же ``topup_amount|<method>|<amount>``, что
    и сетка сумм, и различить входы здесь уже нечем. Поэтому экран, который знает
    свой адрес возврата, кладёт подсказку в FSM.

    Подсказка привязана к сумме: она живёт в состоянии дольше своего сценария, и без
    сверки «Назад» уводил бы на прошлый тариф при следующем обычном пополнении.
    """
    try:
        data = await state.get_data()
    except Exception as error:  # pragma: no cover — состояние недоступно
        logger.warning('Не удалось прочитать адрес возврата апселла', error=error)
        return _amount_screen_callback(method)

    hint = data.get('upsell_back_callback')
    hint_amount = data.get('upsell_back_amount')
    if hint and hint_amount is not None and int(hint_amount) == int(amount_kopeks):
        return str(hint)
    return _amount_screen_callback(method)


def _screen_keyboard(texts, *, back_callback: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('DONATION_UPSELL_ACCEPT', '❤️ Поддержать'),
                    callback_data=UPSELL_ACCEPT,
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('DONATION_UPSELL_DECLINE', 'Не поддержать'),
                    callback_data=UPSELL_DECLINE,
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('DONATION_UPSELL_CUSTOM', '💰 Пожертвовать иную сумму'),
                    callback_data=UPSELL_CUSTOM,
                )
            ],
            # Назад — на экран, с которого пришли. Это отмена, а не отказ: решение по
            # апселлу не записывается (как закрытие модалки в кабинете), иначе уход
            # «передумал с суммой» врал бы в воронке и жёг cooldown.
            [
                types.InlineKeyboardButton(
                    text=texts.BACK,
                    callback_data=back_callback,
                )
            ],
        ]
    )


async def _show(message: types.Message, text: str, keyboard: types.InlineKeyboardMarkup, *, edit: bool) -> int | None:
    """Рисует экран: правкой сообщения, если можем, иначе новым сообщением."""
    if edit:
        try:
            if message.text:
                sent = await message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
            else:
                sent = await message.edit_caption(caption=text, reply_markup=keyboard, parse_mode='HTML')
            return getattr(sent, 'message_id', message.message_id)
        except TelegramBadRequest:
            pass
    sent = await message.answer(text, reply_markup=keyboard, parse_mode='HTML')
    return getattr(sent, 'message_id', None)


async def maybe_show_upsell(
    callback_or_message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    *,
    amount_kopeks: int,
    method: str,
) -> bool:
    """Показывает экран апселла. ``True`` — показан, оплату продолжит его хендлер.

    ``db`` — сессия из auth-middleware: своих сессий здесь не открываем, чтобы не
    плодить параллельные транзакции внутри одного апдейта.
    """
    is_callback = hasattr(callback_or_message, 'data') and hasattr(callback_or_message, 'message')
    message = callback_or_message.message if is_callback else callback_or_message
    if isinstance(message, InaccessibleMessage):
        return False

    try:
        method_max = await _method_max_kopeks(db, method)
        offer = await build_offer(
            db,
            db_user,
            topup_amount_kopeks=amount_kopeks,
            payment_method=method,
            method_max_kopeks=method_max,
        )
    except Exception as error:  # pragma: no cover — апселл не должен ломать пополнение
        logger.warning('Не удалось построить предложение донат-апселла', user_id=db_user.id, error=error)
        return False

    if offer is None:
        # Экрана не будет (cooldown, порог, исключённый способ, лимит), а пополнение
        # начато — значит прежнее обещание к этому платежу не относится и должно
        # погаснуть. Иначе оно дожило бы до совпадения по сумме со следующим счётом.
        await cancel_pending_pledges(db, db_user)
        return False

    texts = get_texts(db_user.language)
    back_callback = await _resolve_back_callback(state, method=method, amount_kopeks=amount_kopeks)
    prompt_message_id = await _show(
        message,
        _screen_text(texts, topup_kopeks=amount_kopeks, tip_kopeks=offer.tip_kopeks),
        _screen_keyboard(texts, back_callback=back_callback),
        edit=is_callback,
    )

    await state.update_data(
        upsell_topup_amount=amount_kopeks,
        upsell_tip=offer.tip_kopeks,
        upsell_method=method,
        upsell_method_max=offer.max_tip_kopeks + amount_kopeks,
        upsell_prompt_message_id=prompt_message_id,
        # Тот же адрес нужен при возврате с экрана «иная сумма» — считаем один раз.
        upsell_back=back_callback,
    )
    return True


async def _send_payment_note(message: types.Message, texts, *, topup_kopeks: int, tip_kopeks: int) -> None:
    """Расшифровка счёта отдельным сообщением: провайдер покажет только итог.

    После согласия человек видит у провайдера «Сумма: 510 ₽» и нигде — из чего она
    состоит: экран апселла к этому моменту уже заменён счётом. Сообщение живёт
    отдельно, чтобы его не снесло вместе с экраном на ветке «сумма текстом».
    """
    if tip_kopeks <= 0:
        return
    try:
        await message.answer(
            texts.t(
                'DONATION_UPSELL_PAYMENT_NOTE',
                '❤️ Счёт на <b>{total}</b>: {topup} — на баланс, {tip} — поддержка проекта.',
            ).format(
                total=texts.format_price(topup_kopeks + tip_kopeks),
                topup=texts.format_price(topup_kopeks),
                tip=texts.format_price(tip_kopeks),
            ),
            parse_mode='HTML',
        )
    except Exception as error:  # pragma: no cover — пояснение не должно ломать оплату
        logger.warning('Не удалось отправить расшифровку суммы апселла', error=error)


async def _dispatch_topup(callback: types.CallbackQuery, db_user: User, state: FSMContext, *, amount_kopeks, method):
    """Продолжает обычный флоу оплаты (импорт локальный — циклическая зависимость)."""
    from .main import dispatch_topup

    return await dispatch_topup(callback, db_user, state, amount_kopeks=amount_kopeks, method=method)


async def _route_payment(message: types.Message, db_user: User, state: FSMContext, *, amount_kopeks, method):
    """Тот же путь, что у обычного текстового ввода суммы (без callback-веток)."""
    from .main import route_payment_by_method

    return await route_payment_by_method(message, db_user, amount_kopeks, state, method)


async def _context(state: FSMContext, message_id: int | None = None) -> tuple[int, int, str] | None:
    """Сумма/надбавка/способ текущего экрана апселла.

    ``message_id`` — сообщение, на котором нажали кнопку. Экран апселла остаётся в
    истории чата, а суммы живут в FSM: без сверки нажатие на СТАРЫЙ экран (человек
    начал новое пополнение, потом пролистал вверх) оплатило бы сумму нового,
    показывая цифры старого. Сверяем, только когда обе стороны известны.
    """
    data = await state.get_data()
    topup = data.get('upsell_topup_amount')
    method = data.get('upsell_method')
    if not topup or not method:
        return None

    prompt_message_id = data.get('upsell_prompt_message_id')
    if message_id is not None and prompt_message_id is not None and int(prompt_message_id) != int(message_id):
        return None

    return int(topup), int(data.get('upsell_tip') or 0), str(method)


@error_handler
async def handle_upsell_accept(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """«Поддержать»: обещание + оплата на увеличенную сумму."""
    context = await _context(state, getattr(callback.message, 'message_id', None))
    if context is None:
        await callback.answer('❌ Сессия оплаты истекла, начните заново', show_alert=True)
        return

    topup_kopeks, tip_kopeks, method = context
    await _accept_with_tip(
        callback, db_user, db, state, topup_kopeks=topup_kopeks, tip_kopeks=tip_kopeks, method=method
    )


async def _accept_with_tip(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    *,
    topup_kopeks: int,
    tip_kopeks: int,
    method: str,
) -> None:
    await register_pledge(
        db,
        db_user,
        tip_kopeks=tip_kopeks,
        topup_amount_kopeks=topup_kopeks,
        payment_method=method,
        source=PLEDGE_SOURCE,
    )

    logger.info(
        '❤️ Пользователь согласился на надбавку',
        user_id=db_user.id,
        tip_kopeks=tip_kopeks,
        topup_amount_kopeks=topup_kopeks,
        method=method,
    )

    await callback.answer()
    if not await _dispatch_topup(callback, db_user, state, amount_kopeks=topup_kopeks + tip_kopeks, method=method):
        await callback.answer('❌ Неизвестный способ оплаты', show_alert=True)
        return

    # Пояснение — после счёта: если способ не сработал, объяснять нечего.
    await _send_payment_note(
        callback.message, get_texts(db_user.language), topup_kopeks=topup_kopeks, tip_kopeks=tip_kopeks
    )


@error_handler
async def handle_upsell_decline(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """«Не поддержать»: отметка отказа + обычная оплата."""
    context = await _context(state, getattr(callback.message, 'message_id', None))
    if context is None:
        await callback.answer('❌ Сессия оплаты истекла, начните заново', show_alert=True)
        return

    topup_kopeks, _tip_kopeks, method = context

    await register_decline(
        db,
        db_user,
        topup_amount_kopeks=topup_kopeks,
        payment_method=method,
        source=PLEDGE_SOURCE,
    )

    await callback.answer()
    if not await _dispatch_topup(callback, db_user, state, amount_kopeks=topup_kopeks, method=method):
        await callback.answer('❌ Неизвестный способ оплаты', show_alert=True)


@error_handler
async def handle_upsell_custom(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """«Иная сумма»: пресеты + приглашение ввести своё число."""
    context = await _context(state, getattr(callback.message, 'message_id', None))
    if context is None:
        await callback.answer('❌ Сессия оплаты истекла, начните заново', show_alert=True)
        return

    topup_kopeks, tip_kopeks, method = context
    texts = get_texts(db_user.language)
    max_tip = resolve_max_tip_kopeks(
        topup_amount_kopeks=topup_kopeks,
        method_max_kopeks=(await state.get_data()).get('upsell_method_max'),
    )

    rows: list[list[types.InlineKeyboardButton]] = []
    row: list[types.InlineKeyboardButton] = []
    for preset in settings.get_donation_upsell_presets_kopeks():
        if preset > max_tip:
            continue
        row.append(
            types.InlineKeyboardButton(
                text=texts.format_price(preset),
                callback_data=f'{UPSELL_CUSTOM_PREFIX}{preset}',
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data=UPSELL_BACK)])

    text = texts.t(
        'DONATION_UPSELL_CUSTOM_PROMPT',
        '💰 <b>Сумма поддержки</b>\n\n'
        'Выберите сумму кнопкой или введите свою в рублях — она добавится к платежу.\n\n'
        '<tg-emoji emoji-id="6244595016287654616">⚠️</tg-emoji> Эта сумма не зачисляется на баланс.',
    )

    await _show(callback.message, text, types.InlineKeyboardMarkup(inline_keyboard=rows), edit=True)
    await state.set_state(BalanceStates.waiting_for_upsell_tip)
    await state.update_data(upsell_tip=tip_kopeks, upsell_method=method, upsell_topup_amount=topup_kopeks)
    await callback.answer()


@error_handler
async def handle_upsell_custom_amount(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Пресет надбавки нажат кнопкой."""
    context = await _context(state, getattr(callback.message, 'message_id', None))
    if context is None:
        await callback.answer('❌ Сессия оплаты истекла, начните заново', show_alert=True)
        return

    topup_kopeks, _tip_kopeks, method = context
    try:
        tip_kopeks = int(callback.data[len(UPSELL_CUSTOM_PREFIX) :])
    except ValueError:
        await callback.answer('❌ Некорректная сумма', show_alert=True)
        return

    await state.set_state(BalanceStates.waiting_for_amount)
    await _accept_with_tip(
        callback, db_user, db, state, topup_kopeks=topup_kopeks, tip_kopeks=tip_kopeks, method=method
    )


@error_handler
async def handle_upsell_back(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Возврат с экрана «иная сумма» к предложению по умолчанию."""
    context = await _context(state, getattr(callback.message, 'message_id', None))
    if context is None:
        await callback.answer('❌ Сессия оплаты истекла, начните заново', show_alert=True)
        return

    topup_kopeks, tip_kopeks, method = context
    texts = get_texts(db_user.language)
    back_callback = (await state.get_data()).get('upsell_back') or _amount_screen_callback(method)
    await _show(
        callback.message,
        _screen_text(texts, topup_kopeks=topup_kopeks, tip_kopeks=tip_kopeks),
        _screen_keyboard(texts, back_callback=back_callback),
        edit=True,
    )
    await state.set_state(BalanceStates.waiting_for_amount)
    await callback.answer()


@error_handler
async def process_upsell_custom_amount(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Надбавка, введённая текстом."""
    texts = get_texts(db_user.language)
    context = await _context(state)
    if context is None:
        await state.clear()
        await message.answer(texts.t('DONATION_UPSELL_EXPIRED', 'Сессия оплаты истекла, начните пополнение заново.'))
        return

    topup_kopeks, _tip_kopeks, method = context
    data = await state.get_data()

    try:
        tip_rubles = float((message.text or '').strip().replace(',', '.'))
        # 'inf'/'nan' парсятся float'ом молча, а round(inf) бросает OverflowError
        # мимо except ниже — пользователь получал бы общую «Произошла ошибка».
        if not math.isfinite(tip_rubles):
            raise ValueError('не конечное число')
        tip_kopeks = int(round(tip_rubles * 100))
    except (ValueError, OverflowError):
        await message.answer(texts.INVALID_AMOUNT)
        return

    if tip_kopeks <= 0:
        await message.answer(texts.INVALID_AMOUNT)
        return

    max_tip = resolve_max_tip_kopeks(
        topup_amount_kopeks=topup_kopeks,
        method_max_kopeks=data.get('upsell_method_max'),
    )
    if tip_kopeks > max_tip:
        await message.answer(
            texts.t(
                'DONATION_UPSELL_TIP_TOO_BIG',
                '❌ Для этого способа оплаты можно добавить не больше {amount}.',
            ).format(amount=texts.format_price(max_tip))
        )
        return

    # Убираем сообщение с числом и экран запроса, чтобы в чате остался только счёт.
    for chat_id, message_id in (
        (message.chat.id, message.message_id),
        (message.chat.id, data.get('upsell_prompt_message_id')),
    ):
        if not message_id:
            continue
        try:
            await message.bot.delete_message(chat_id, message_id)
        except Exception as delete_error:  # pragma: no cover — зависит от прав бота
            logger.warning('Не удалось удалить сообщение апселла', error=delete_error)

    await register_pledge(
        db,
        db_user,
        tip_kopeks=tip_kopeks,
        topup_amount_kopeks=topup_kopeks,
        payment_method=method,
        source=PLEDGE_SOURCE,
    )

    # payment_method в state обязателен: часть провайдеров читает из него подметод
    # (СБП/карта). На этом пути в state лежат только upsell_*, поэтому проставляем явно —
    # иначе провайдер выбрал бы подметод от прошлого экрана.
    await state.update_data(payment_method=method)
    await state.set_state(BalanceStates.waiting_for_amount)

    if not await _route_payment(message, db_user, state, amount_kopeks=topup_kopeks + tip_kopeks, method=method):
        await message.answer('Неизвестный способ оплаты')
        return

    await _send_payment_note(message, texts, topup_kopeks=topup_kopeks, tip_kopeks=tip_kopeks)


def register_donation_upsell_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_upsell_accept, F.data == UPSELL_ACCEPT)
    dp.callback_query.register(handle_upsell_decline, F.data == UPSELL_DECLINE)
    dp.callback_query.register(handle_upsell_custom, F.data == UPSELL_CUSTOM)
    dp.callback_query.register(handle_upsell_back, F.data == UPSELL_BACK)
    dp.callback_query.register(handle_upsell_custom_amount, F.data.startswith(UPSELL_CUSTOM_PREFIX))
    # ~F.successful_payment: сообщение об оплате Stars не должно попасть в ввод суммы
    # (тот же приём, что в обычном пополнении и донатах).
    dp.message.register(process_upsell_custom_amount, BalanceStates.waiting_for_upsell_tip, ~F.successful_payment)
