"""A single first-purchase offer 48 hours after registration."""

from asyncio import timeout
from datetime import UTC, datetime, timedelta

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.models import DiscountOffer, Transaction, TransactionType, User, UserStatus
from app.services.notification_settings_service import NotificationSettingsService


logger = structlog.get_logger(__name__)
OFFER_TYPE = 'first_purchase_48h'


def paid_purchase_exists(user_id):
    return (
        select(Transaction.id)
        .where(
            Transaction.user_id == user_id,
            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            Transaction.is_completed.is_(True),
            Transaction.amount_kopeks > 0,
        )
        .exists()
    )


async def is_first_purchase_eligible(db, user_id: int) -> bool:
    return bool(
        await db.scalar(
            select(User.id).where(
                User.id == user_id,
                User.status == UserStatus.ACTIVE.value,
                User.has_had_paid_subscription.is_(False),
                User.restriction_subscription.is_(False),
                ~paid_purchase_exists(user_id),
            )
        )
    )


async def send_first_purchase_offers(db, bot) -> int:
    if not settings.FIRST_PURCHASE_OFFER_ENABLED or not bot:
        return 0
    if not NotificationSettingsService.are_notifications_globally_enabled():
        return 0
    now = datetime.now(UTC)
    # Only the registration cohort reaching 48 h now, not the entire historical base.
    ids = (
        await db.scalars(
            select(User.id)
            .where(
                User.created_at <= now - timedelta(hours=48),
                User.created_at > now - timedelta(hours=72),
                User.telegram_id.is_not(None),
                User.status == UserStatus.ACTIVE.value,
                User.has_had_paid_subscription.is_(False),
                ~paid_purchase_exists(User.id),
                ~select(DiscountOffer.id)
                .where(
                    DiscountOffer.user_id == User.id,
                    DiscountOffer.notification_type == OFFER_TYPE,
                )
                .exists(),
            )
            .order_by(User.id)
            .limit(200)
        )
    ).all()
    sent = 0
    for user_id in ids:
        # Same user lock as balance purchases: recheck after a concurrent purchase.
        user = await db.scalar(
            select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
        )
        if not user or not await is_first_purchase_eligible(db, user_id):
            await db.rollback()
            continue
        telegram_id = user.telegram_id
        expires_at = datetime.now(UTC) + timedelta(hours=24)
        offer = DiscountOffer(
            user_id=user_id,
            subscription_id=None,
            notification_type=OFFER_TYPE,
            discount_percent=10,
            bonus_amount_kopeks=0,
            effect_type='percent_discount',
            expires_at=expires_at,
            is_active=True,
        )
        db.add(offer)
        try:
            await db.flush()
            offer_id = offer.id
            # A durable unique offer prevents another worker or restart sending a second one.
            await db.commit()
        except IntegrityError:
            await db.rollback()
            continue
        if not await is_first_purchase_eligible(db, user_id):
            await db.rollback()
            continue
        try:
            async with timeout(10):
                await bot.send_message(
                    telegram_id,
                    '🎁 Скидка 10% на первую подписку!\n\nВы с нами уже два дня — самое время попробовать. '
                    'Предложение действует 24 часа. Нажмите кнопку, чтобы активировать скидку.',
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text='🎁 Получить скидку 10%', callback_data=f'claim_discount_{offer_id}'
                                ),
                            ]
                        ]
                    ),
                )
            sent += 1
        except Exception as error:
            logger.warning('First purchase offer delivery failed', user_id=user_id, error_type=type(error).__name__)
        await db.rollback()
    return sent
