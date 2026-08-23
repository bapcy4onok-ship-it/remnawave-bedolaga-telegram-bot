import hashlib
import json
from html import escape as html_escape
from pathlib import Path

import qrcode
import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PartnerStatus, User
from app.keyboards.inline import get_referral_keyboard
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService, NotificationCategory
from app.services.partner_application_service import partner_application_service
from app.services.referral_withdrawal_service import referral_withdrawal_service
from app.states import PartnerApplicationStates, ReferralWithdrawalStates
from app.utils.photo_message import edit_or_answer_photo
from app.utils.user_utils import (
    get_detailed_referral_list,
    get_effective_referral_commission_percent,
    get_referral_analytics,
    get_user_referral_summary,
)


logger = structlog.get_logger(__name__)


async def show_referral_info(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    # Проверяем, включена ли реферальная программа
    if not settings.is_referral_program_enabled():
        texts = get_texts(db_user.language)
        await callback.answer(texts.t('REFERRAL_PROGRAM_DISABLED', 'Реферальная программа отключена'), show_alert=True)
        return

    texts = get_texts(db_user.language)

    if not db_user.referral_code:
        await callback.answer(texts.t('REFERRAL_CODE_NOT_ASSIGNED', 'Реферальный код не назначен'), show_alert=True)
        return

    summary = await get_user_referral_summary(db, db_user.id)

    bot_username = (await callback.bot.get_me()).username
    bot_referral_link = settings.get_bot_referral_link(db_user.referral_code, bot_username)
    cabinet_referral_link = settings.get_cabinet_referral_link(db_user.referral_code)

    referral_text = (
        texts.t('REFERRAL_PROGRAM_TITLE', '👥 <b>Реферальная программа</b>')
        + '\n\n'
        + texts.t('REFERRAL_STATS_HEADER', '<tg-emoji emoji-id="5877256784267186004">📊</tg-emoji> <b>Ваша статистика:</b>')
        + '\n\n'
        + texts.t(
            'REFERRAL_STATS_INVITED',
            '• Приглашено пользователей: <b>{count}</b>',
        ).format(count=summary['invited_count'])
        + '\n'
        + texts.t(
            'REFERRAL_STATS_FIRST_TOPUPS',
            '• Сделали первое пополнение: <b>{count}</b>',
        ).format(count=summary['paid_referrals_count'])
        + '\n'
        + texts.t(
            'REFERRAL_STATS_ACTIVE',
            '• Активных рефералов: <b>{count}</b>',
        ).format(count=summary['active_referrals_count'])
        + '\n'
        + texts.t(
            'REFERRAL_STATS_CONVERSION',
            '• Конверсия: <b>{rate}%</b>',
        ).format(rate=summary['conversion_rate'])
        + '\n'
        + texts.t(
            'REFERRAL_STATS_TOTAL_EARNED',
            '• Заработано всего: <b>{amount}</b>',
        ).format(amount=texts.format_price(summary['total_earned_kopeks']))
        + '\n'
        + texts.t(
            'REFERRAL_STATS_MONTH_EARNED',
            '• За последний месяц: <b>{amount}</b>',
        ).format(amount=texts.format_price(summary['month_earned_kopeks']))
        + '\n\n'
        + texts.t('REFERRAL_REWARDS_HEADER', '🎁 <b>Как работают награды:</b>')
        + '\n'
    )

    if settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS > 0:
        referral_text += '\n' + texts.t(
            'REFERRAL_REWARD_NEW_USER',
            '• Новый пользователь получает: <b>{bonus}</b> при первом пополнении от <b>{minimum}</b>',
        ).format(
            bonus=texts.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
            minimum=texts.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
        )

    # Партнёрам фиксированный бонус не начисляется — строку не показываем
    if settings.REFERRAL_INVITER_BONUS_KOPEKS > 0 and not db_user.is_partner:
        referral_text += '\n' + texts.t(
            'REFERRAL_REWARD_INVITER',
            '• Вы получаете при первом пополнении реферала: <b>{bonus}</b>',
        ).format(bonus=texts.format_price(settings.REFERRAL_INVITER_BONUS_KOPEKS))

    if settings.REFERRAL_MAX_COMMISSION_PAYMENTS > 0:
        commission_line = texts.t(
            'REFERRAL_REWARD_COMMISSION_LIMITED',
            '• Комиссия с первых {max_payments} пополнений реферала: <b>{percent}%</b>',
        ).format(
            percent=get_effective_referral_commission_percent(db_user),
            max_payments=settings.REFERRAL_MAX_COMMISSION_PAYMENTS,
        )
    else:
        commission_line = texts.t(
            'REFERRAL_REWARD_COMMISSION',
            '• Комиссия с каждого пополнения реферала: <b>{percent}%</b>',
        ).format(percent=get_effective_referral_commission_percent(db_user))

    referral_text += '\n' + commission_line + '\n\n'

    # Show bot link
    referral_text += (
        texts.t('REFERRAL_BOT_LINK_TITLE', '🤖 <b>Ссылка на бота:</b>') + f'\n\n{html_escape(bot_referral_link)}\n'
    )

    # Show cabinet link if configured
    if cabinet_referral_link:
        referral_text += (
            '\n'
            + texts.t('REFERRAL_CABINET_LINK_TITLE', '<tg-emoji emoji-id="5449855298627989865">🌐</tg-emoji> <b>Ссылка на сайт:</b>')
            + f'\n\n{html_escape(cabinet_referral_link)}\n'
        )

    referral_text += (
        '\n'
        + texts.t('REFERRAL_CODE_TITLE', '🆔 <b>Ваш код:</b> <code>{code}</code>').format(
            code=html_escape(str(db_user.referral_code or ''))
        )
        + '\n\n'
    )

    if summary['recent_earnings']:
        meaningful_earnings = [earning for earning in summary['recent_earnings'][:5] if earning['amount_kopeks'] > 0]

        if meaningful_earnings:
            referral_text += (
                texts.t(
                    'REFERRAL_RECENT_EARNINGS_HEADER',
                    '💰 <b>Последние начисления:</b>',
                )
                + '\n'
            )
            for earning in meaningful_earnings[:3]:
                reason_text = {
                    'referral_first_topup': texts.t(
                        'REFERRAL_EARNING_REASON_FIRST_TOPUP',
                        '🎉 Первое пополнение',
                    ),
                    'referral_commission_topup': texts.t(
                        'REFERRAL_EARNING_REASON_COMMISSION_TOPUP',
                        '💰 Комиссия с пополнения',
                    ),
                    'referral_commission': texts.t(
                        'REFERRAL_EARNING_REASON_COMMISSION_PURCHASE',
                        '💰 Комиссия с покупки',
                    ),
                }.get(earning['reason'], earning['reason'])

                referral_text += (
                    texts.t(
                        'REFERRAL_RECENT_EARNINGS_ITEM',
                        '• {reason}: <b>{amount}</b> от {referral_name}',
                    ).format(
                        reason=reason_text,
                        amount=texts.format_price(earning['amount_kopeks']),
                        referral_name=html_escape(str(earning['referral_name'] or '')),
                    )
                    + '\n'
                )
            referral_text += '\n'

    if summary['earnings_by_type']:
        referral_text += (
            texts.t(
                'REFERRAL_EARNINGS_BY_TYPE_HEADER',
                '📈 <b>Доходы по типам:</b>',
            )
            + '\n'
        )

        if 'referral_first_topup' in summary['earnings_by_type']:
            data = summary['earnings_by_type']['referral_first_topup']
            if data['total_amount_kopeks'] > 0:
                referral_text += (
                    texts.t(
                        'REFERRAL_EARNINGS_FIRST_TOPUPS',
                        '• Бонусы за первые пополнения: <b>{count}</b> ({amount})',
                    ).format(
                        count=data['count'],
                        amount=texts.format_price(data['total_amount_kopeks']),
                    )
                    + '\n'
                )

        if 'referral_commission_topup' in summary['earnings_by_type']:
            data = summary['earnings_by_type']['referral_commission_topup']
            if data['total_amount_kopeks'] > 0:
                referral_text += (
                    texts.t(
                        'REFERRAL_EARNINGS_TOPUPS',
                        '• Комиссии с пополнений: <b>{count}</b> ({amount})',
                    ).format(
                        count=data['count'],
                        amount=texts.format_price(data['total_amount_kopeks']),
                    )
                    + '\n'
                )

        if 'referral_commission' in summary['earnings_by_type']:
            data = summary['earnings_by_type']['referral_commission']
            if data['total_amount_kopeks'] > 0:
                referral_text += (
                    texts.t(
                        'REFERRAL_EARNINGS_PURCHASES',
                        '• Комиссии с покупок: <b>{count}</b> ({amount})',
                    ).format(
                        count=data['count'],
                        amount=texts.format_price(data['total_amount_kopeks']),
                    )
                    + '\n'
                )

        referral_text += '\n'

    referral_text += texts.t(
        'REFERRAL_INVITE_FOOTER',
        '📢 Приглашайте друзей и зарабатывайте!',
    )

    await edit_or_answer_photo(
        callback,
        referral_text,
        get_referral_keyboard(db_user.language, partner_status=db_user.partner_status),
    )
    await callback.answer()


async def show_referral_qr(
    callback: types.CallbackQuery,
    db_user: User,
):
    texts = get_texts(db_user.language)

    if not db_user.referral_code:
        await callback.answer(texts.t('REFERRAL_CODE_NOT_ASSIGNED', 'Реферальный код не назначен'), show_alert=True)
        return

    await callback.answer()

    bot_username = (await callback.bot.get_me()).username
    bot_referral_link = settings.get_bot_referral_link(db_user.referral_code, bot_username)

    qr_dir = Path('data') / 'referral_qr'
    qr_dir.mkdir(parents=True, exist_ok=True)

    link_hash = hashlib.md5(bot_referral_link.encode()).hexdigest()[:8]
    file_path = qr_dir / f'{db_user.id}_{link_hash}.png'
    if not file_path.exists():
        img = qrcode.make(bot_referral_link)
        img.save(file_path)

    photo = FSInputFile(file_path)
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
    )

    caption = texts.t(
        'REFERRAL_QR_BOT_LINK',
        '🤖 Ссылка на бота:\n{link}',
    ).format(link=bot_referral_link)

    cabinet_referral_link = settings.get_cabinet_referral_link(db_user.referral_code)
    if cabinet_referral_link:
        caption += '\n\n' + texts.t(
            'REFERRAL_QR_CABINET_LINK',
            '<tg-emoji emoji-id="5449855298627989865">🌐</tg-emoji> Ссылка на сайт:\n{link}',
        ).format(link=cabinet_referral_link)

    try:
        await callback.message.edit_media(
            types.InputMediaPhoto(media=photo, caption=caption),
            reply_markup=keyboard,
        )
    except TelegramBadRequest:
        await callback.message.delete()
        await callback.message.answer_photo(
            photo,
            caption=caption,
            reply_markup=keyboard,
        )


async def show_detailed_referral_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, page: int = 1):
    texts = get_texts(db_user.language)

    referrals_data = await get_detailed_referral_list(db, db_user.id, limit=10, offset=(page - 1) * 10)

    if not referrals_data['referrals']:
        await edit_or_answer_photo(
            callback,
            texts.t(
                'REFERRAL_LIST_EMPTY',
                '📋 У вас пока нет рефералов.\n\nПоделитесь своей реферальной ссылкой, чтобы начать зарабатывать!',
            ),
            types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
            ),
            parse_mode=None,
        )
        await callback.answer()
        return

    text = (
        texts.t(
            'REFERRAL_LIST_HEADER',
            '👥 <b>Ваши рефералы</b> (стр. {current}/{total})',
        ).format(
            current=referrals_data['current_page'],
            total=referrals_data['total_pages'],
        )
        + '\n\n'
    )

    for i, referral in enumerate(referrals_data['referrals'], 1):
        status_emoji = '🟢' if referral['status'] == 'active' else '🔴'

        topup_emoji = '💰' if referral['has_made_first_topup'] else '<tg-emoji emoji-id="5269539162654010758">⏳</tg-emoji>'

        text += (
            texts.t(
                'REFERRAL_LIST_ITEM_HEADER',
                '{index}. {status} <b>{name}</b>',
            ).format(index=i, status=status_emoji, name=html_escape(str(referral['full_name'] or '')))
            + '\n'
        )
        text += (
            texts.t(
                'REFERRAL_LIST_ITEM_TOPUPS',
                '   {emoji} Пополнений: {count}',
            ).format(emoji=topup_emoji, count=referral['topups_count'])
            + '\n'
        )
        text += (
            texts.t(
                'REFERRAL_LIST_ITEM_EARNED',
                '   <tg-emoji emoji-id="5462902520215002477">💎</tg-emoji> Заработано с него: {amount}',
            ).format(amount=texts.format_price(referral['total_earned_kopeks']))
            + '\n'
        )
        text += (
            texts.t(
                'REFERRAL_LIST_ITEM_REGISTERED',
                '   📅 Регистрация: {days} дн. назад',
            ).format(days=referral['days_since_registration'])
            + '\n'
        )

        if referral['days_since_activity'] is not None:
            text += (
                texts.t(
                    'REFERRAL_LIST_ITEM_ACTIVITY',
                    '   🕐 Активность: {days} дн. назад',
                ).format(days=referral['days_since_activity'])
                + '\n'
            )
        else:
            text += (
                texts.t(
                    'REFERRAL_LIST_ITEM_ACTIVITY_LONG_AGO',
                    '   🕐 Активность: давно',
                )
                + '\n'
            )

        text += '\n'

    keyboard = []
    nav_buttons = []

    if referrals_data['has_prev']:
        nav_buttons.append(
            types.InlineKeyboardButton(
                text=texts.t('REFERRAL_LIST_PREV_PAGE', '⬅️ Назад'), callback_data=f'referral_list_page_{page - 1}'
            )
        )

    if referrals_data['has_next']:
        nav_buttons.append(
            types.InlineKeyboardButton(
                text=texts.t('REFERRAL_LIST_NEXT_PAGE', 'Вперед ➡️'), callback_data=f'referral_list_page_{page + 1}'
            )
        )

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')])

    await edit_or_answer_photo(
        callback,
        text,
        types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


async def show_referral_analytics(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)

    analytics = await get_referral_analytics(db, db_user.id)

    text = texts.t('REFERRAL_ANALYTICS_TITLE', '<tg-emoji emoji-id="5877256784267186004">📊</tg-emoji> <b>Аналитика рефералов</b>') + '\n\n'

    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_HEADER',
            '💰 <b>Доходы по периодам:</b>',
        )
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_TODAY',
            '• Сегодня: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['today']))
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_WEEK',
            '• За неделю: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['week']))
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_MONTH',
            '• За месяц: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['month']))
        + '\n'
    )
    text += (
        texts.t(
            'REFERRAL_ANALYTICS_EARNINGS_QUARTER',
            '• За квартал: {amount}',
        ).format(amount=texts.format_price(analytics['earnings_by_period']['quarter']))
        + '\n\n'
    )

    if analytics['top_referrals']:
        text += (
            texts.t(
                'REFERRAL_ANALYTICS_TOP_TITLE',
                '🏆 <b>Топ-{count} рефералов:</b>',
            ).format(count=len(analytics['top_referrals']))
            + '\n'
        )
        for i, ref in enumerate(analytics['top_referrals'], 1):
            text += (
                texts.t(
                    'REFERRAL_ANALYTICS_TOP_ITEM',
                    '{index}. {name}: {amount} ({count} начислений)',
                ).format(
                    index=i,
                    name=html_escape(str(ref['referral_name'] or '')),
                    amount=texts.format_price(ref['total_earned_kopeks']),
                    count=ref['earnings_count'],
                )
                + '\n'
            )
        text += '\n'

    text += texts.t(
        'REFERRAL_ANALYTICS_FOOTER',
        '📈 Продолжайте развивать свою реферальную сеть!',
    )

    await edit_or_answer_photo(
        callback,
        text,
        types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
        ),
    )
    await callback.answer()


async def create_invite_message(callback: types.CallbackQuery, db_user: User):
    texts = get_texts(db_user.language)

    if not db_user.referral_code:
        await callback.answer(texts.t('REFERRAL_CODE_NOT_ASSIGNED', 'Реферальный код не назначен'), show_alert=True)
        return

    bot_username = (await callback.bot.get_me()).username
    bot_referral_link = settings.get_bot_referral_link(db_user.referral_code, bot_username)
    cabinet_referral_link = settings.get_cabinet_referral_link(db_user.referral_code)

    bonus_block = ''
    if settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS > 0:
        bonus_block = '\n\n' + texts.t(
            'REFERRAL_INVITE_BONUS',
            '<tg-emoji emoji-id="5462902520215002477">💎</tg-emoji> При первом пополнении от {minimum} ты получишь {bonus} бонусом на баланс!',
        ).format(
            minimum=texts.format_price(settings.REFERRAL_MINIMUM_TOPUP_KOPEKS),
            bonus=texts.format_price(settings.REFERRAL_FIRST_TOPUP_BONUS_KOPEKS),
        )

    # Ссылки оборачиваем в <code>: при тапе по <blockquote> для копирования
    # Telegram сохраняет содержимое <code> в буфере, но ВЫБРАСЫВАЕТ авто-линкнутые
    # сырые URL — поэтому из скопированного приглашения выпадали обе ссылки
    # (#634720). Экранируем прозу сами и подставляем ссылки уже в <code>, а не
    # html_escape-им всю собранную строку (иначе экранировались бы и теги <code>).
    cabinet_block = ''
    if cabinet_referral_link:
        cabinet_block = f'\n\n<tg-emoji emoji-id="5449855298627989865">🌐</tg-emoji> <code>{html_escape(cabinet_referral_link)}</code>'

    invite_template = texts.t(
        'REFERRAL_INVITE_TEXT',
        '🎉 Присоединяйся к VPN сервису!{bonus_block}\n\n'
        '🚀 Быстрое подключение\n'
        '<tg-emoji emoji-id="5465166522030764559">🌍</tg-emoji> Серверы по всему миру\n'
        '🔒 Надежная защита\n\n'
        '👇 Переходи по ссылке:\n'
        '{link}{cabinet_block}',
    )
    invite_html = html_escape(invite_template).format(
        bonus_block=html_escape(bonus_block),
        link=f'<code>{html_escape(bot_referral_link)}</code>',
        cabinet_block=cabinet_block,
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')],
        ]
    )

    await edit_or_answer_photo(
        callback,
        (
            texts.t('REFERRAL_INVITE_CREATED_TITLE', '📝 <b>Приглашение создано!</b>')
            + '\n\n'
            + texts.t(
                'REFERRAL_INVITE_CREATED_INSTRUCTION',
                'Нажмите на текст ниже, чтобы скопировать:',
            )
            + '\n\n'
            f'<blockquote>{invite_html}</blockquote>'
        ),
        keyboard,
    )
    await callback.answer()


async def show_withdrawal_info(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Показывает информацию о выводе реферального баланса."""
    texts = get_texts(db_user.language)

    if not settings.is_referral_withdrawal_enabled():
        await callback.answer(texts.t('REFERRAL_WITHDRAWAL_DISABLED', 'Функция вывода отключена'), show_alert=True)
        return

    # Получаем детальную статистику баланса
    stats = await referral_withdrawal_service.get_referral_balance_stats(db, db_user.id)
    min_amount = settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS
    cooldown_days = settings.REFERRAL_WITHDRAWAL_COOLDOWN_DAYS

    # Проверяем возможность вывода
    can_request, reason, _stats = await referral_withdrawal_service.can_request_withdrawal(db, db_user.id)

    text = texts.t('REFERRAL_WITHDRAWAL_TITLE', '💸 <b>Вывод реферального баланса</b>') + '\n\n'

    # Показываем детальную статистику
    text += referral_withdrawal_service.format_balance_stats_for_user(stats, texts)
    text += '\n'

    text += (
        texts.t('REFERRAL_WITHDRAWAL_MIN_AMOUNT', '<tg-emoji emoji-id="5877256784267186004">📊</tg-emoji> Минимальная сумма: <b>{amount}</b>').format(
            amount=texts.format_price(min_amount)
        )
        + '\n'
    )
    text += (
        texts.t('REFERRAL_WITHDRAWAL_COOLDOWN', '⏱ Частота вывода: раз в <b>{days}</b> дней').format(days=cooldown_days)
        + '\n\n'
    )

    keyboard = []

    if can_request:
        text += texts.t('REFERRAL_WITHDRAWAL_READY', '✅ Вы можете запросить вывод средств') + '\n'
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_WITHDRAWAL_REQUEST_BUTTON', '📝 Оформить заявку'),
                    callback_data='referral_withdrawal_start',
                )
            ]
        )
    else:
        text += f'❌ {html_escape(str(reason))}\n'

    keyboard.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')])

    await edit_or_answer_photo(callback, text, types.InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


async def start_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Начинает процесс оформления заявки на вывод."""
    texts = get_texts(db_user.language)

    # Повторная проверка
    can_request, reason, wd_stats = await referral_withdrawal_service.can_request_withdrawal(db, db_user.id)
    if not can_request:
        await callback.answer(reason, show_alert=True)
        return

    available = wd_stats.get('available_total', 0) if wd_stats else 0

    # Сохраняем доступный баланс в состоянии
    await state.update_data(available_balance=available)
    await state.set_state(ReferralWithdrawalStates.waiting_for_amount)

    text = texts.t(
        'REFERRAL_WITHDRAWAL_ENTER_AMOUNT', '💸 Введите сумму для вывода в рублях\n\nДоступно: <b>{amount}</b>'
    ).format(amount=texts.format_price(available))

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_WITHDRAWAL_ALL', f'Вывести всё ({available / 100:.0f}₽)'),
                    callback_data=f'referral_withdrawal_amount_{available}',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                )
            ],
        ]
    )

    await edit_or_answer_photo(callback, text, keyboard)
    await callback.answer()


async def process_withdrawal_amount(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Обрабатывает ввод суммы для вывода."""
    texts = get_texts(db_user.language)
    data = await state.get_data()
    available = data.get('available_balance', 0)

    try:
        # Парсим сумму (в рублях)
        amount_text = message.text.strip().replace(',', '.').replace('₽', '').replace(' ', '')
        amount_rubles = float(amount_text)
        amount_kopeks = int(amount_rubles * 100)

        if amount_kopeks <= 0:
            await message.answer(texts.t('REFERRAL_WITHDRAWAL_INVALID_AMOUNT', '❌ Введите положительную сумму'))
            return

        min_amount = settings.REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS
        if amount_kopeks < min_amount:
            await message.answer(
                texts.t('REFERRAL_WITHDRAWAL_MIN_ERROR', '❌ Минимальная сумма: {amount}').format(
                    amount=texts.format_price(min_amount)
                )
            )
            return

        if amount_kopeks > available:
            await message.answer(
                texts.t('REFERRAL_WITHDRAWAL_INSUFFICIENT', '❌ Недостаточно средств. Доступно: {amount}').format(
                    amount=texts.format_price(available)
                )
            )
            return

        # Сохраняем сумму и переходим к вводу реквизитов
        await state.update_data(withdrawal_amount=amount_kopeks)
        await state.set_state(ReferralWithdrawalStates.waiting_for_payment_details)

        text = texts.t(
            'REFERRAL_WITHDRAWAL_ENTER_DETAILS',
            '💳 Введите реквизиты для перевода:\n\nНапример:\n• СБП: +7 999 123-45-67 (Сбербанк)',
        )

        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                    )
                ]
            ]
        )

        await message.answer(text, reply_markup=keyboard)

    except ValueError:
        await message.answer(texts.t('REFERRAL_WITHDRAWAL_INVALID_AMOUNT', '❌ Введите корректную сумму'))


async def process_withdrawal_amount_callback(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Обрабатывает выбор суммы для вывода через кнопку."""
    texts = get_texts(db_user.language)

    # Получаем сумму из callback_data
    amount_kopeks = int(callback.data.split('_')[-1])

    # Сохраняем сумму и переходим к вводу реквизитов
    await state.update_data(withdrawal_amount=amount_kopeks)
    await state.set_state(ReferralWithdrawalStates.waiting_for_payment_details)

    text = texts.t(
        'REFERRAL_WITHDRAWAL_ENTER_DETAILS',
        '💳 Введите реквизиты для перевода:\n\nНапример:\n• СБП: +7 999 123-45-67 (Сбербанк)',
    )

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                )
            ]
        ]
    )

    await edit_or_answer_photo(callback, text, keyboard)
    await callback.answer()


async def process_payment_details(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Обрабатывает ввод реквизитов и показывает подтверждение."""
    texts = get_texts(db_user.language)
    data = await state.get_data()
    amount_kopeks = data.get('withdrawal_amount', 0)
    payment_details = message.text.strip()

    if len(payment_details) < 10:
        await message.answer(texts.t('REFERRAL_WITHDRAWAL_DETAILS_TOO_SHORT', '❌ Реквизиты слишком короткие'))
        return

    # Сохраняем реквизиты
    await state.update_data(payment_details=payment_details)
    await state.set_state(ReferralWithdrawalStates.confirming)

    text = texts.t('REFERRAL_WITHDRAWAL_CONFIRM_TITLE', '📋 <b>Подтверждение заявки</b>') + '\n\n'
    text += (
        texts.t('REFERRAL_WITHDRAWAL_CONFIRM_AMOUNT', '💰 Сумма: <b>{amount}</b>').format(
            amount=texts.format_price(amount_kopeks)
        )
        + '\n\n'
    )
    text += (
        texts.t('REFERRAL_WITHDRAWAL_CONFIRM_DETAILS', '💳 Реквизиты:\n<code>{details}</code>').format(
            details=html_escape(payment_details)
        )
        + '\n\n'
    )
    text += texts.t('REFERRAL_WITHDRAWAL_CONFIRM_WARNING', '<tg-emoji emoji-id="6244595016287654616">⚠️</tg-emoji> После отправки заявка будет рассмотрена администрацией')

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_WITHDRAWAL_CONFIRM_BUTTON', '✅ Подтвердить'),
                    callback_data='referral_withdrawal_confirm',
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_withdrawal_cancel'
                )
            ],
        ]
    )

    await message.answer(text, reply_markup=keyboard)


async def confirm_withdrawal_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Подтверждает и создаёт заявку на вывод."""
    texts = get_texts(db_user.language)
    data = await state.get_data()
    amount_kopeks = data.get('withdrawal_amount', 0)
    payment_details = data.get('payment_details', '')

    await state.clear()

    # Создаём заявку
    request, error = await referral_withdrawal_service.create_withdrawal_request(
        db, db_user.id, amount_kopeks, payment_details
    )

    if error:
        await callback.answer(f'❌ {error}', show_alert=True)
        return

    # Отправляем уведомление админам
    analysis = json.loads(request.risk_analysis) if request.risk_analysis else {}

    user_id_display = html_escape(str(db_user.telegram_id or db_user.email or f'#{db_user.id}'))
    safe_name = html_escape(db_user.full_name or 'Без имени')
    safe_details = html_escape(payment_details)
    admin_text = f"""
🔔 <b>Новая заявка на вывод #{request.id}</b>

👤 Пользователь: {safe_name}
🆔 ID: <code>{user_id_display}</code>
💰 Сумма: <b>{amount_kopeks / 100:.0f}₽</b>

💳 Реквизиты:
<code>{safe_details}</code>

{referral_withdrawal_service.format_analysis_for_admin(analysis)}
"""

    # Формируем клавиатуру - кнопка профиля только для Telegram-пользователей
    keyboard_rows = [
        [
            types.InlineKeyboardButton(text='✅ Одобрить', callback_data=f'admin_withdrawal_approve_{request.id}'),
            types.InlineKeyboardButton(text='❌ Отклонить', callback_data=f'admin_withdrawal_reject_{request.id}'),
        ]
    ]
    if db_user.telegram_id:
        keyboard_rows.append(
            [
                types.InlineKeyboardButton(
                    text='👤 Профиль пользователя', callback_data=f'admin_user_{db_user.telegram_id}'
                )
            ]
        )
    admin_keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    try:
        notification_service = AdminNotificationService(callback.bot)
        await notification_service.send_admin_notification(
            admin_text, reply_markup=admin_keyboard, category=NotificationCategory.PARTNERS
        )
    except Exception as e:
        logger.error('Ошибка отправки уведомления админам о заявке на вывод', error=e)

    # Уведомление в топик, если настроено
    topic_id = settings.REFERRAL_WITHDRAWAL_NOTIFICATIONS_TOPIC_ID
    if topic_id and settings.ADMIN_NOTIFICATIONS_CHAT_ID:
        try:
            await callback.bot.send_message(
                chat_id=settings.ADMIN_NOTIFICATIONS_CHAT_ID,
                message_thread_id=topic_id,
                text=admin_text,
                reply_markup=admin_keyboard,
                parse_mode='HTML',
            )
        except Exception as e:
            logger.error('Ошибка отправки уведомления в топик о заявке на вывод', error=e)

    # Отвечаем пользователю
    text = texts.t(
        'REFERRAL_WITHDRAWAL_SUCCESS',
        '✅ <b>Заявка #{id} создана!</b>\n\n'
        'Сумма: <b>{amount}</b>\n\n'
        'Ваша заявка будет рассмотрена администрацией. '
        'Мы уведомим вас о результате.',
    ).format(id=request.id, amount=texts.format_price(amount_kopeks))

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
    )

    await edit_or_answer_photo(callback, text, keyboard)
    await callback.answer()


async def cancel_withdrawal_request(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """Отменяет процесс создания заявки на вывод."""
    await state.clear()
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('CANCELLED', 'Отменено'))

    # Возвращаем в меню партнёрки
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
    )
    await edit_or_answer_photo(callback, texts.t('REFERRAL_WITHDRAWAL_CANCELLED', '❌ Заявка отменена'), keyboard)


# ==================== ЗАЯВКА НА ПАРТНЁРСТВО ====================


def _partner_back_keyboard(texts) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data='menu_referrals')]]
    )


def _partner_step_keyboard(texts) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_PARTNER_SKIP_STEP', '⏭️ Пропустить'),
                    callback_data='referral_partner_skip',
                )
            ],
            [types.InlineKeyboardButton(text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_partner_cancel')],
        ]
    )


# Шаги заявки — те же поля и порядок, что в форме личного кабинета
_PARTNER_APPLY_STEPS: list[dict] = [
    {
        'key': 'partner_company_name',
        'state': PartnerApplicationStates.waiting_for_company,
        'kind': 'text',
        'max_len': 255,
        'title': ('REFERRAL_PARTNER_FIELD_COMPANY', '🏢 Название компании'),
        'hint': ('REFERRAL_PARTNER_FIELD_COMPANY_HINT', 'Название вашей компании или бренда'),
    },
    {
        'key': 'partner_telegram_channel',
        'state': PartnerApplicationStates.waiting_for_channel,
        'kind': 'text',
        'max_len': 255,
        'title': ('REFERRAL_PARTNER_FIELD_CHANNEL', '📢 Telegram-канал'),
        'hint': ('REFERRAL_PARTNER_FIELD_CHANNEL_HINT', 'Например: @вашканал'),
    },
    {
        'key': 'partner_website_url',
        'state': PartnerApplicationStates.waiting_for_website,
        'kind': 'text',
        'max_len': 500,
        'title': ('REFERRAL_PARTNER_FIELD_WEBSITE', '<tg-emoji emoji-id="5449855298627989865">🌐</tg-emoji> Сайт'),
        'hint': ('REFERRAL_PARTNER_FIELD_WEBSITE_HINT', 'Например: https://example.com'),
    },
    {
        'key': 'partner_description',
        'state': PartnerApplicationStates.waiting_for_description,
        'kind': 'text',
        'max_len': 2000,
        'title': ('REFERRAL_PARTNER_FIELD_DESCRIPTION', '📝 Описание'),
        'hint': ('REFERRAL_PARTNER_FIELD_DESCRIPTION_HINT', 'Расскажите о вашей аудитории и планах продвижения'),
    },
    {
        'key': 'partner_expected_referrals',
        'state': PartnerApplicationStates.waiting_for_expected,
        'kind': 'int',
        'min_value': 0,
        'max_value': 2_000_000_000,
        'title': ('REFERRAL_PARTNER_FIELD_EXPECTED', '👥 Ожидаемое количество рефералов в месяц'),
        'hint': ('REFERRAL_PARTNER_FIELD_EXPECTED_HINT', 'Отправьте примерное количество в месяц (число)'),
    },
    {
        'key': 'partner_desired_commission',
        'state': PartnerApplicationStates.waiting_for_commission,
        'kind': 'int',
        'min_value': 1,
        'max_value': 100,
        'title': ('REFERRAL_PARTNER_FIELD_COMMISSION', '💰 Желаемый процент комиссии'),
        'hint': ('REFERRAL_PARTNER_FIELD_COMMISSION_HINT', 'Отправьте число от 1 до 100'),
    },
]

_PARTNER_STATE_TO_INDEX = {step['state'].state: i for i, step in enumerate(_PARTNER_APPLY_STEPS)}


def _partner_step_text(texts, index: int) -> str:
    step = _PARTNER_APPLY_STEPS[index]
    return (
        texts.t('REFERRAL_PARTNER_STEP', '<b>Шаг {current} из {total}</b>').format(
            current=index + 1, total=len(_PARTNER_APPLY_STEPS)
        )
        + '\n\n'
        + texts.t(*step['title'])
        + '\n'
        + texts.t(*step['hint'])
        + '\n\n'
        + texts.t('REFERRAL_PARTNER_OPTIONAL_HINT', 'Поле необязательное — его можно пропустить.')
    )


def _partner_confirm_text(texts, data: dict) -> str:
    lines = [texts.t('REFERRAL_PARTNER_CONFIRM_TITLE', '🤝 <b>Проверьте заявку</b>'), '']

    filled = False
    for step in _PARTNER_APPLY_STEPS:
        value = data.get(step['key'])
        if value is None or value == '':
            continue
        filled = True
        lines.append(f'{texts.t(*step["title"])}: {html_escape(str(value))}')

    if not filled:
        lines.append(
            texts.t(
                'REFERRAL_PARTNER_ALL_SKIPPED',
                'Все поля пропущены — заявка будет подана только с данными вашего профиля.',
            )
        )

    lines += ['', texts.t('REFERRAL_PARTNER_CONFIRM_QUESTION', 'Отправить заявку?')]
    return '\n'.join(lines)


def _partner_confirm_keyboard(texts) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('REFERRAL_PARTNER_SUBMIT_BUTTON', '✅ Отправить'),
                    callback_data='referral_partner_confirm',
                )
            ],
            [types.InlineKeyboardButton(text=texts.t('CANCEL', '❌ Отмена'), callback_data='referral_partner_cancel')],
        ]
    )


async def _partner_advance(
    state: FSMContext,
    texts,
    index: int,
    *,
    message: types.Message | None = None,
    callback: types.CallbackQuery | None = None,
):
    """Переходит к следующему шагу заявки или к подтверждению."""
    next_index = index + 1

    if next_index < len(_PARTNER_APPLY_STEPS):
        await state.set_state(_PARTNER_APPLY_STEPS[next_index]['state'])
        text = _partner_step_text(texts, next_index)
        keyboard = _partner_step_keyboard(texts)
    else:
        await state.set_state(PartnerApplicationStates.confirming)
        data = await state.get_data()
        text = _partner_confirm_text(texts, data)
        keyboard = _partner_confirm_keyboard(texts)

    if message is not None:
        await message.answer(text, reply_markup=keyboard)
    else:
        await edit_or_answer_photo(callback, text, keyboard)


async def show_partner_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Показывает статус заявки на партнёрство."""
    texts = get_texts(db_user.language)

    application = await partner_application_service.get_latest_application(db, db_user.id)

    if db_user.partner_status == PartnerStatus.APPROVED.value:
        text = texts.t('REFERRAL_PARTNER_APPROVED_TEXT', '🤝 Вы являетесь партнёром проекта!')
    elif db_user.partner_status == PartnerStatus.PENDING.value:
        text = texts.t(
            'REFERRAL_PARTNER_PENDING_TEXT',
            '<tg-emoji emoji-id="5269539162654010758">⏳</tg-emoji> <b>Заявка на партнёрство на рассмотрении</b>\n\nМы уведомим вас о решении.',
        )
        if application and application.created_at:
            text += '\n\n' + texts.t('REFERRAL_PARTNER_APPLIED_AT', '📅 Подана: {date}').format(
                date=application.created_at.strftime('%d.%m.%Y')
            )
    else:
        text = texts.t('REFERRAL_PARTNER_NO_APPLICATION', 'У вас нет активной заявки.')

    await edit_or_answer_photo(callback, text, _partner_back_keyboard(texts))
    await callback.answer()


async def start_partner_application(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Начинает подачу заявки на партнёрство."""
    texts = get_texts(db_user.language)

    if db_user.partner_status == PartnerStatus.APPROVED.value:
        await callback.answer(texts.t('REFERRAL_PARTNER_ALREADY', 'Вы уже являетесь партнёром'), show_alert=True)
        return
    if db_user.partner_status == PartnerStatus.PENDING.value:
        await callback.answer(
            texts.t('REFERRAL_PARTNER_PENDING_ALERT', 'Ваша заявка уже на рассмотрении'), show_alert=True
        )
        return

    text = texts.t(
        'REFERRAL_PARTNER_APPLY_INTRO',
        '🤝 <b>Заявка на партнёрство</b>\n\n'
        'Заполните анкету для подачи заявки на партнёрскую программу. '
        'Все поля необязательные.',
    )

    if db_user.partner_status == PartnerStatus.REJECTED.value:
        application = await partner_application_service.get_latest_application(db, db_user.id)
        if application and application.admin_comment:
            text += '\n\n' + texts.t(
                'REFERRAL_PARTNER_REJECTED_COMMENT',
                '💬 Комментарий по прошлой заявке: {comment}',
            ).format(comment=html_escape(application.admin_comment))

    await state.set_state(_PARTNER_APPLY_STEPS[0]['state'])

    text += '\n\n' + _partner_step_text(texts, 0)
    await edit_or_answer_photo(callback, text, _partner_step_keyboard(texts))
    await callback.answer()


async def process_partner_step(message: types.Message, db_user: User, state: FSMContext):
    """Принимает значение текущего шага анкеты."""
    current_state = await state.get_state()
    index = _PARTNER_STATE_TO_INDEX.get(current_state)
    if index is None:
        return

    texts = get_texts(db_user.language)
    step = _PARTNER_APPLY_STEPS[index]
    raw = (message.text or '').strip()

    if step['kind'] == 'int':
        try:
            value = int(raw)
        except (TypeError, ValueError):
            await message.answer(texts.t('REFERRAL_PARTNER_NUMBER_INVALID', '❌ Отправьте число'))
            return
        if value < step['min_value'] or value > step['max_value']:
            await message.answer(
                texts.t('REFERRAL_PARTNER_NUMBER_RANGE', '❌ Число должно быть от {min} до {max}').format(
                    min=step['min_value'], max=step['max_value']
                )
            )
            return
    else:
        if not raw:
            await message.answer(
                texts.t('REFERRAL_PARTNER_TEXT_EMPTY', '❌ Отправьте текст или нажмите «Пропустить»')
            )
            return
        if len(raw) > step['max_len']:
            await message.answer(
                texts.t('REFERRAL_PARTNER_TEXT_TOO_LONG', '❌ Слишком длинный текст (максимум {max} символов)').format(
                    max=step['max_len']
                )
            )
            return
        value = raw

    await state.update_data(**{step['key']: value})
    await _partner_advance(state, texts, index, message=message)


async def skip_partner_step(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """Пропускает текущий шаг анкеты."""
    current_state = await state.get_state()
    index = _PARTNER_STATE_TO_INDEX.get(current_state)
    if index is None:
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    step = _PARTNER_APPLY_STEPS[index]
    await state.update_data(**{step['key']: None})
    await _partner_advance(state, texts, index, callback=callback)
    await callback.answer()


async def confirm_partner_application(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Отправляет заявку на партнёрство."""
    texts = get_texts(db_user.language)
    data = await state.get_data()

    application, error = await partner_application_service.submit_application(
        db,
        user_id=db_user.id,
        company_name=data.get('partner_company_name'),
        website_url=data.get('partner_website_url'),
        telegram_channel=data.get('partner_telegram_channel'),
        description=data.get('partner_description'),
        expected_monthly_referrals=data.get('partner_expected_referrals'),
        desired_commission_percent=data.get('partner_desired_commission'),
    )

    await state.clear()

    if not application:
        await callback.answer(error or texts.t('ERROR_OCCURRED', 'Произошла ошибка'), show_alert=True)
        return

    try:
        notification_service = AdminNotificationService(callback.bot)
        await notification_service.send_partner_application_notification(
            user=db_user,
            application_data={
                'company_name': application.company_name,
                'telegram_channel': application.telegram_channel,
                'website_url': application.website_url,
                'description': application.description,
                'expected_monthly_referrals': application.expected_monthly_referrals,
                'desired_commission_percent': application.desired_commission_percent,
            },
        )
    except Exception as e:
        logger.error('Не удалось отправить уведомление о заявке на партнёрство', error=e)

    await edit_or_answer_photo(
        callback,
        texts.t(
            'REFERRAL_PARTNER_SUBMITTED',
            '✅ <b>Заявка отправлена!</b>\n\nМы рассмотрим её и сообщим о решении.',
        ),
        _partner_back_keyboard(texts),
    )
    await callback.answer()


async def cancel_partner_application(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """Отменяет подачу заявки на партнёрство."""
    await state.clear()
    texts = get_texts(db_user.language)
    await callback.answer(texts.t('CANCELLED', 'Отменено'))
    await edit_or_answer_photo(
        callback,
        texts.t('REFERRAL_PARTNER_CANCELLED', '❌ Подача заявки отменена'),
        _partner_back_keyboard(texts),
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_referral_info, F.data == 'menu_referrals')

    dp.callback_query.register(create_invite_message, F.data == 'referral_create_invite')

    dp.callback_query.register(show_referral_qr, F.data == 'referral_show_qr')

    dp.callback_query.register(show_detailed_referral_list, F.data == 'referral_list')

    dp.callback_query.register(show_referral_analytics, F.data == 'referral_analytics')

    async def handle_referral_list_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
        page = int(callback.data.split('_')[-1])
        await show_detailed_referral_list(callback, db_user, db, page)

    dp.callback_query.register(handle_referral_list_page, F.data.startswith('referral_list_page_'))

    # Хендлеры вывода реферального баланса
    dp.callback_query.register(show_withdrawal_info, F.data == 'referral_withdrawal')

    dp.callback_query.register(start_withdrawal_request, F.data == 'referral_withdrawal_start')

    dp.callback_query.register(process_withdrawal_amount_callback, F.data.startswith('referral_withdrawal_amount_'))

    dp.callback_query.register(confirm_withdrawal_request, F.data == 'referral_withdrawal_confirm')

    dp.callback_query.register(cancel_withdrawal_request, F.data == 'referral_withdrawal_cancel')

    # Обработка текстового ввода суммы
    dp.message.register(process_withdrawal_amount, ReferralWithdrawalStates.waiting_for_amount)

    # Заявка на партнёрство
    dp.callback_query.register(start_partner_application, F.data == 'referral_partner_apply')
    dp.callback_query.register(show_partner_status, F.data == 'referral_partner_status')
    dp.callback_query.register(
        skip_partner_step, F.data == 'referral_partner_skip', StateFilter(PartnerApplicationStates)
    )
    dp.callback_query.register(
        confirm_partner_application, F.data == 'referral_partner_confirm', PartnerApplicationStates.confirming
    )
    dp.callback_query.register(cancel_partner_application, F.data == 'referral_partner_cancel')
    dp.message.register(process_partner_step, StateFilter(PartnerApplicationStates))

    # Обработка текстового ввода реквизитов
    dp.message.register(process_payment_details, ReferralWithdrawalStates.waiting_for_payment_details)
