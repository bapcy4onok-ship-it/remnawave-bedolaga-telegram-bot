"""Channel membership required for trial activation, independent of menu access."""

from contextlib import asynccontextmanager

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import HTTPException

from app.bot_factory import create_bot
from app.config import settings
from app.services.channel_subscription_service import channel_subscription_service


def is_channel_member(member) -> bool:
    return member.status in ('member', 'administrator', 'creator') or (
        member.status == 'restricted' and member.is_member
    )


@asynccontextmanager
async def _bot_or_create(bot):
    if bot is not None:
        yield bot
    else:
        async with create_bot() as owned:
            yield owned


async def missing_trial_channels(user, bot=None) -> list[dict]:
    if not settings.CHANNEL_TRIAL_REQUIRED:
        return []
    channels = await channel_subscription_service.get_required_channels()
    # A missing channel configuration must not silently issue a free trial.
    if not channels or not user.telegram_id:
        raise HTTPException(
            403,
            detail={
                'code': 'trial_channel_required',
                'message': 'Для пробного периода привяжите Telegram и подпишитесь на канал.',
            },
        )
    missing = []
    async with _bot_or_create(bot) as client:
        for channel in channels:
            try:
                member = await client.get_chat_member(chat_id=int(channel['channel_id']), user_id=user.telegram_id)
            except Exception:
                raise HTTPException(
                    503,
                    detail={
                        'code': 'channel_check_failed',
                        'message': 'Не удалось проверить подписку на канал. Попробуйте позже.',
                    },
                ) from None
            if not is_channel_member(member):
                missing.append(channel)
    return missing


async def require_trial_channels(user, bot=None) -> None:
    channels = await missing_trial_channels(user, bot)
    if channels:
        raise HTTPException(
            403,
            detail={
                'code': 'trial_channel_required',
                'message': 'Для пробного периода подпишитесь на канал.',
                'channels': channels,
            },
        )


async def show_trial_channel_gate(callback, user) -> bool:
    try:
        channels = await missing_trial_channels(user, callback.bot)
    except HTTPException as error:
        await callback.answer(error.detail['message'], show_alert=True)
        return True
    if not channels:
        return False
    rows = [
        [InlineKeyboardButton(text=ch.get('title') or 'Подписаться на канал', url=ch['channel_link'])]
        for ch in channels
        if ch.get('channel_link')
    ]
    rows.append([InlineKeyboardButton(text='🔄 Проверить и получить пробный период', callback_data='trial_activate')])
    rows.append([InlineKeyboardButton(text='⬅️ В меню', callback_data='back_to_menu')])
    await callback.message.edit_text(
        '🎁 Для получения пробного периода подпишитесь на канал. При отписке пробная подписка отключается.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()
    return True
