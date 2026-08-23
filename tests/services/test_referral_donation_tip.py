"""Реферальные начисления не считаются с надбавки донат-апселла.

Надбавка входит в платёж, но на баланс не попадает: пост-хук списывает её сразу
после реферальной логики. Без поправки реферер получал бы процент с чужого
пожертвования, а порог первого пополнения брался бы за счёт надбавки.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.services.donation_upsell_service as upsell
from app.services import referral_service


def _referral_env(monkeypatch, *, tip_kopeks: int, has_made_first_topup: bool):
    """Реферал с реферером, все побочки замоканы; peek возвращает ``tip_kopeks``."""
    user = SimpleNamespace(
        id=1,
        telegram_id=101,
        full_name='Referral User',
        referred_by_id=2,
        has_made_first_topup=has_made_first_topup,
    )
    referrer = SimpleNamespace(
        id=2,
        telegram_id=202,
        full_name='Referrer',
        email=None,
        is_partner=False,
    )
    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())

    monkeypatch.setattr(referral_service, 'get_user_by_id', AsyncMock(side_effect=[user, referrer]))
    add_user_balance = AsyncMock(return_value=True)
    monkeypatch.setattr(referral_service, 'add_user_balance', add_user_balance)
    create_referral_earning = AsyncMock()
    monkeypatch.setattr(referral_service, 'create_referral_earning', create_referral_earning)
    monkeypatch.setattr(referral_service, 'get_commission_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(referral_service, 'get_user_campaign_id', AsyncMock(return_value=None))
    monkeypatch.setattr(referral_service, 'get_referral_reward_payment_count', AsyncMock(return_value=0))
    monkeypatch.setattr(referral_service, 'get_effective_referral_commission_percent', lambda u: 15)

    peek = AsyncMock(return_value=tip_kopeks)
    monkeypatch.setattr(upsell, 'peek_pending_tip_kopeks', peek)

    monkeypatch.setattr(referral_service.settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 10000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 5000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 5000)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_COMMISSION_PERCENT', 15)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_FIRST_PAYMENT_COMMISSION_PERCENT', None)
    monkeypatch.setattr(referral_service.settings, 'REFERRAL_RECURRING_COMMISSION_TIERS', '')

    return SimpleNamespace(
        db=db,
        user=user,
        referrer=referrer,
        add_user_balance=add_user_balance,
        create_referral_earning=create_referral_earning,
        peek=peek,
    )


async def test_commission_is_calculated_without_donation_tip(monkeypatch):
    """Пополнение 500 ₽ + надбавка 10 ₽: комиссия считается с 500, а не с 510."""
    env = _referral_env(monkeypatch, tip_kopeks=1000, has_made_first_topup=True)

    result = await referral_service.process_referral_topup(env.db, env.user.id, 51000)

    assert result is True
    env.peek.assert_awaited_once()
    assert env.peek.await_args.kwargs['credited_amount_kopeks'] == 51000

    env.add_user_balance.assert_awaited_once()
    assert env.add_user_balance.await_args.args[2] == 7500  # 15% от 50000, а не 7650
    assert env.create_referral_earning.await_args.kwargs['amount_kopeks'] == 7500


async def test_donation_tip_does_not_drag_referral_over_minimum(monkeypatch):
    """Порог первого пополнения берётся от чистой суммы: 95 ₽ + 10 ₽ надбавки < 100 ₽."""
    env = _referral_env(monkeypatch, tip_kopeks=1000, has_made_first_topup=False)

    result = await referral_service.process_referral_topup(env.db, env.user.id, 10500)

    assert result is True
    # Первое пополнение не засчитано — право на бонус сохраняется до настоящих 100 ₽.
    assert env.user.has_made_first_topup is False
    # Но комиссия с пополнения начисляется, и тоже с чистой суммы.
    env.add_user_balance.assert_awaited_once()
    assert env.add_user_balance.await_args.args[2] == 1425  # 15% от 9500
    assert env.create_referral_earning.await_args.kwargs['reason'] == 'referral_commission_topup'


async def test_full_amount_used_when_no_pledge_matches(monkeypatch):
    """Обычное пополнение без обещания — поведение прежнее, ничего не вычитается."""
    env = _referral_env(monkeypatch, tip_kopeks=0, has_made_first_topup=True)

    await referral_service.process_referral_topup(env.db, env.user.id, 51000)

    env.peek.assert_awaited_once()
    assert env.add_user_balance.await_args.args[2] == 7650  # 15% от полных 51000


async def test_direct_subscription_purchase_keeps_full_amount(monkeypatch):
    """Прямая покупка подписки: баланс не пополняется, надбавку списывать нечем."""
    env = _referral_env(monkeypatch, tip_kopeks=1000, has_made_first_topup=True)

    await referral_service.process_referral_topup(env.db, env.user.id, 51000, exclude_donation_tip=False)

    env.peek.assert_not_awaited()
    assert env.add_user_balance.await_args.args[2] == 7650


async def test_tip_never_makes_amount_negative(monkeypatch):
    """Надбавка больше платежа (рассинхрон данных) не должна давать отрицательную базу."""
    env = _referral_env(monkeypatch, tip_kopeks=99000, has_made_first_topup=True)

    result = await referral_service.process_referral_topup(env.db, env.user.id, 51000)

    assert result is True
    env.add_user_balance.assert_not_awaited()  # комиссия с нуля не начисляется
    env.create_referral_earning.assert_not_awaited()


@pytest.mark.parametrize('exclude', [True, False])
async def test_non_referral_user_is_untouched(monkeypatch, exclude):
    """У пользователя без реферера ничего не считается — и в БД за обещанием не ходим."""
    user = SimpleNamespace(id=1, telegram_id=101, full_name='Solo', referred_by_id=None)
    monkeypatch.setattr(referral_service, 'get_user_by_id', AsyncMock(return_value=user))
    peek = AsyncMock(return_value=1000)
    monkeypatch.setattr(upsell, 'peek_pending_tip_kopeks', peek)

    result = await referral_service.process_referral_topup(
        SimpleNamespace(commit=AsyncMock(), execute=AsyncMock()),
        user.id,
        51000,
        exclude_donation_tip=exclude,
    )

    assert result is True
    peek.assert_not_awaited()
