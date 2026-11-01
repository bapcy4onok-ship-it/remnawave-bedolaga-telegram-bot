from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.models import DiscountOffer, Transaction, User
from app.services import first_purchase_offer_service as service
from tests.fixtures.sqlite_memory import memory_session


TABLES = [User.__table__, Transaction.__table__, DiscountOffer.__table__]


@pytest.mark.asyncio
async def test_only_48h_unpaid_users_receive_one_offer(monkeypatch):
    monkeypatch.setattr(settings, 'FIRST_PURCHASE_OFFER_ENABLED', True)
    monkeypatch.setattr(service.NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        db.add_all(
            [
                User(
                    id=i,
                    telegram_id=1000 + i,
                    first_name='Test',
                    created_at=now - timedelta(hours=age),
                    has_had_paid_subscription=(i == 4),
                )
                for i, age in [(1, 49), (2, 47), (3, 73), (4, 49), (5, 49)]
            ]
        )
        db.add(Transaction(user_id=5, type='subscription_payment', amount_kopeks=100, is_completed=True))
        await db.commit()
        bot = AsyncMock()
        assert await service.send_first_purchase_offers(db, bot) == 1
        assert await service.send_first_purchase_offers(db, bot) == 0
        offers = (await db.scalars(select(DiscountOffer))).all()
        assert len(offers) == 1
        offer = offers[0]
        assert offer.user_id == 1
        assert offer.discount_percent == 10
        assert timedelta(hours=23, minutes=59) < offer.expires_at - now <= timedelta(hours=24, seconds=5)
        bot.send_message.assert_awaited_once()
        user = await db.get(User, 1)
        user.has_had_paid_subscription = True
        await db.commit()
        assert not await service.is_first_purchase_eligible(db, 1)


@pytest.mark.asyncio
async def test_database_prevents_second_offer_even_after_claim_or_expiry(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        db.add(User(id=1, telegram_id=1001, first_name='Test'))
        await db.commit()
        for active in [False, True]:
            db.add(
                DiscountOffer(
                    user_id=1,
                    notification_type=service.OFFER_TYPE,
                    discount_percent=10,
                    expires_at=datetime.now(UTC),
                    is_active=active,
                )
            )
            if not active:
                await db.commit()
            else:
                with pytest.raises(IntegrityError):
                    await db.commit()
                await db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize('already_paid', [False, True])
async def test_claim_rechecks_purchase_and_preserves_original_deadline(monkeypatch, already_paid):
    from fastapi import HTTPException

    from app.cabinet.routes import promo

    async with memory_session(monkeypatch, TABLES) as db:
        user = User(id=1, telegram_id=1001, first_name='Test', has_had_paid_subscription=already_paid)
        deadline = datetime.now(UTC) + timedelta(hours=1)
        offer = DiscountOffer(
            user_id=1, notification_type=service.OFFER_TYPE, discount_percent=10, expires_at=deadline, is_active=True
        )
        db.add_all([user, offer])
        await db.commit()
        monkeypatch.setattr(promo, 'get_offer_by_id', AsyncMock(return_value=offer))

        async def commit_claim(*args, **kwargs):
            await db.commit()

        monkeypatch.setattr(promo, 'mark_offer_claimed', AsyncMock(side_effect=commit_claim))
        request = promo.ClaimOfferRequest(offer_id=offer.id)
        if already_paid:
            with pytest.raises(HTTPException) as error:
                await promo.claim_promo_offer(request, user, db)
            assert error.value.status_code == 400
            promo.mark_offer_claimed.assert_not_awaited()
        else:
            result = await promo.claim_promo_offer(request, user, db)
            assert result.success
            assert result.discount_percent == 10
            assert result.expires_at == deadline
            assert user.promo_offer_discount_expires_at == deadline
