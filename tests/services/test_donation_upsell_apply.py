"""Исполнение обещания: надбавка уходит из баланса в пожертвование ровно один раз."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.donation_upsell_service as upsell
from app.config import settings
from app.database.models import DonationPledgeStatus, TransactionType


def _pledge(pledge_id: int = 1, tip: int = 1000, total: int = 51000):
    pledge = MagicMock()
    pledge.id = pledge_id
    pledge.tip_kopeks = tip
    pledge.expected_total_kopeks = total
    pledge.topup_amount_kopeks = total - tip
    pledge.payment_method = 'yookassa'
    pledge.expires_at = datetime.now(UTC) + timedelta(hours=1)
    return pledge


def _user(balance: int = 51000):
    user = MagicMock()
    user.id = 42
    user.telegram_id = 111
    user.language = 'ru'
    user.balance_kopeks = balance
    return user


def test_matching_pledge_exact_amount():
    pledge = _pledge()

    assert upsell.select_matching_pledge([pledge], credited_amount_kopeks=51000) is pledge


def test_matching_pledge_tolerates_small_drift():
    """Курсовой пересчёт у CryptoBot двигает сумму на копейки — допуск это покрывает."""
    pledge = _pledge(total=51000)

    assert upsell.select_matching_pledge([pledge], credited_amount_kopeks=50980) is pledge


def test_no_match_for_different_amount():
    pledge = _pledge(total=51000)

    assert upsell.select_matching_pledge([pledge], credited_amount_kopeks=10000) is None


def test_no_match_when_tip_is_zero():
    assert upsell.select_matching_pledge([_pledge(tip=0)], credited_amount_kopeks=51000) is None


def test_newest_matching_pledge_wins():
    older = _pledge(pledge_id=1)
    newer = _pledge(pledge_id=2)

    assert upsell.select_matching_pledge([newer, older], credited_amount_kopeks=51000) is newer


def test_plain_topup_of_base_amount_never_matches():
    """Регрессия: допуск обязан быть СТРОГО меньше надбавки.

    Пользователь согласился на +10 ₽ к 1000 ₽ (обещание ждёт 1010 ₽), но платёж
    бросил. Потом пополнил на те же 1000 ₽ без апселла. Если допуск считать как
    1% от суммы (10.10 ₽), он перекроет надбавку — и с честного пополнения
    спишется чужая надбавка. Допуск обязан быть меньше tip_kopeks.
    """
    pledge = _pledge(tip=1000, total=101000)  # пополнение 1000 ₽ + надбавка 10 ₽

    assert upsell.select_matching_pledge([pledge], credited_amount_kopeks=100000) is None


def test_tolerance_never_reaches_tip_for_small_tips():
    """Надбавка меньше базового допуска: окно сверки сжимается под неё."""
    pledge = _pledge(tip=50, total=50050)

    assert upsell.select_matching_pledge([pledge], credited_amount_kopeks=50000) is None
    assert upsell.select_matching_pledge([pledge], credited_amount_kopeks=50050) is pledge


def test_external_id_format():
    assert upsell.pledge_external_id(7) == 'pledge:7'


@pytest.fixture
def applied_env(monkeypatch):
    """Общая обвязка: обещание найдено, отметка проходит, побочки замоканы."""
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)

    pledge = _pledge()
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', AsyncMock(return_value=[pledge]))
    monkeypatch.setattr(upsell.pledge_crud, 'mark_pledge_applied', AsyncMock(return_value=True))
    monkeypatch.setattr(upsell.pledge_crud, 'mark_pledge_status', AsyncMock())

    transaction = MagicMock(id=555)
    create_transaction = AsyncMock(return_value=transaction)
    create_donation = AsyncMock()
    lock_user = AsyncMock(side_effect=lambda db, user: user)
    notify = AsyncMock()

    monkeypatch.setattr(upsell, 'create_transaction', create_transaction, raising=False)
    monkeypatch.setattr(upsell, 'create_donation', create_donation, raising=False)
    monkeypatch.setattr(upsell, 'lock_user_for_update', lock_user, raising=False)
    monkeypatch.setattr(upsell, '_notify_pledge_applied', notify, raising=False)

    return {
        'pledge': pledge,
        'create_transaction': create_transaction,
        'create_donation': create_donation,
        'notify': notify,
    }


@pytest.mark.asyncio
async def test_apply_debits_balance_and_records_donation(applied_env):
    db = AsyncMock()
    user = _user(balance=51000)

    applied = await upsell.try_apply_donation_pledge(db, user, credited_amount_kopeks=51000, bot=MagicMock())

    assert applied == 1000  # вернулась списанная надбавка — её вычитает письмо о пополнении
    assert user.balance_kopeks == 50000
    tx_kwargs = applied_env['create_transaction'].await_args.kwargs
    assert tx_kwargs['type'] == TransactionType.DONATION
    assert tx_kwargs['amount_kopeks'] == 1000
    assert tx_kwargs['external_id'] == 'pledge:1'
    donation_kwargs = applied_env['create_donation'].await_args.kwargs
    assert donation_kwargs['amount_kopeks'] == 1000
    assert donation_kwargs['source'] == upsell.DONATION_UPSELL_SOURCE
    assert donation_kwargs['external_id'] == 'pledge:1'
    assert donation_kwargs['transaction_id'] == 555
    db.commit.assert_awaited()
    applied_env['notify'].assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_is_idempotent(applied_env, monkeypatch):
    """Повторный вебхук: отметка не прошла → откат, баланс не тронут."""
    monkeypatch.setattr(upsell.pledge_crud, 'mark_pledge_applied', AsyncMock(return_value=False))
    db = AsyncMock()
    user = _user(balance=51000)

    applied = await upsell.try_apply_donation_pledge(db, user, credited_amount_kopeks=51000)

    assert applied == 0
    assert user.balance_kopeks == 51000
    db.rollback.assert_awaited()
    applied_env['create_donation'].assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_transaction_is_not_an_error(applied_env, monkeypatch):
    """Повторный вебхук ловится уникальным индексом — это INFO, а не ERROR."""
    from sqlalchemy.exc import IntegrityError

    monkeypatch.setattr(
        upsell,
        'create_transaction',
        AsyncMock(side_effect=IntegrityError('INSERT', {}, Exception('duplicate key'))),
        raising=False,
    )
    db = AsyncMock()
    user = _user(balance=51000)

    applied = await upsell.try_apply_donation_pledge(db, user, credited_amount_kopeks=51000)

    assert applied == 0
    assert user.balance_kopeks == 51000
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_apply_skipped_when_balance_too_low(applied_env, monkeypatch):
    mark_status = AsyncMock()
    monkeypatch.setattr(upsell.pledge_crud, 'mark_pledge_status', mark_status)
    db = AsyncMock()
    user = _user(balance=500)

    applied = await upsell.try_apply_donation_pledge(db, user, credited_amount_kopeks=51000)

    assert applied == 0
    assert user.balance_kopeks == 500
    assert mark_status.await_args.kwargs['status'] == DonationPledgeStatus.SKIPPED


@pytest.mark.asyncio
async def test_apply_noop_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', False, raising=False)
    list_pending = AsyncMock(return_value=[])
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', list_pending)

    applied = await upsell.try_apply_donation_pledge(AsyncMock(), _user(), credited_amount_kopeks=51000)

    assert applied == 0
    list_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_noop_without_session(monkeypatch):
    """Хук зовут и с db=None (ручное пополнение в тестах) — не шуметь в логах."""
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)
    list_pending = AsyncMock(return_value=[])
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', list_pending)

    applied = await upsell.try_apply_donation_pledge(None, _user(), credited_amount_kopeks=51000)

    assert applied == 0
    list_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_noop_when_no_pledge(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', AsyncMock(return_value=[]))

    applied = await upsell.try_apply_donation_pledge(AsyncMock(), _user(), credited_amount_kopeks=51000)

    assert applied == 0


@pytest.mark.asyncio
async def test_rollback_revives_caller_user_object(applied_env, monkeypatch):
    """Регрессия: откат нашей надбавки не должен ронять остальной пост-хук.

    db — сессия вызывающего провайдера, и его user живёт в той же identity map.
    Голый rollback() экспайрит инстанс, после чего чтение user.telegram_id в
    send_cart_notification_after_topup падает с MissingGreenlet, и автовыкуп
    сохранённой корзины не выполняется: человек заплатил и не получил подписку.
    """
    monkeypatch.setattr(upsell.pledge_crud, 'mark_pledge_applied', AsyncMock(return_value=False))
    db = AsyncMock()
    user = _user(balance=51000)

    await upsell.try_apply_donation_pledge(db, user, credited_amount_kopeks=51000)

    db.rollback.assert_awaited()
    db.refresh.assert_awaited_with(user)


@pytest.mark.asyncio
async def test_unmatched_pledge_is_logged(applied_env, monkeypatch):
    """Обещание есть, сумма не сошлась — расхождение должно быть видно в логах."""
    logged = []
    monkeypatch.setattr(upsell.logger, 'info', lambda *a, **kw: logged.append((a, kw)))

    applied = await upsell.try_apply_donation_pledge(AsyncMock(), _user(), credited_amount_kopeks=12345)

    assert applied == 0
    assert any('не совпала' in str(a) for a, _ in logged)


def test_cryptobot_excluded_from_upsell():
    """Курс USD->RUB пересчитывается в момент оплаты — обещание неисполнимо."""
    assert 'cryptobot' in upsell.EXCLUDED_UPSELL_METHODS
    assert 'tribute' in upsell.EXCLUDED_UPSELL_METHODS


# --- peek_pending_tip_kopeks: взгляд вперёд для реферальной комиссии ---------------


@pytest.mark.asyncio
async def test_peek_returns_tip_of_matching_pledge(monkeypatch):
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', AsyncMock(return_value=[_pledge()]))

    tip = await upsell.peek_pending_tip_kopeks(AsyncMock(), user_id=42, credited_amount_kopeks=51000)

    assert tip == 1000


@pytest.mark.asyncio
async def test_peek_returns_zero_when_amount_does_not_match(monkeypatch):
    """Плата не по обещанию — комиссию рефереру резать не за что."""
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', AsyncMock(return_value=[_pledge()]))

    assert await upsell.peek_pending_tip_kopeks(AsyncMock(), user_id=42, credited_amount_kopeks=50000) == 0


@pytest.mark.asyncio
async def test_peek_agrees_with_apply(monkeypatch):
    """Матчер общий: сколько peek обещал вычесть, столько apply и списывает."""
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)
    pledge = _pledge()
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', AsyncMock(return_value=[pledge]))
    monkeypatch.setattr(upsell.pledge_crud, 'mark_pledge_applied', AsyncMock(return_value=True))
    monkeypatch.setattr(upsell, 'create_transaction', AsyncMock(return_value=MagicMock(id=1)), raising=False)
    monkeypatch.setattr(upsell, 'create_donation', AsyncMock(), raising=False)
    monkeypatch.setattr(upsell, 'lock_user_for_update', AsyncMock(side_effect=lambda db, user: user), raising=False)

    peeked = await upsell.peek_pending_tip_kopeks(AsyncMock(), user_id=42, credited_amount_kopeks=50980)
    applied = await upsell.try_apply_donation_pledge(AsyncMock(), _user(), credited_amount_kopeks=50980)

    assert peeked == applied == 1000


@pytest.mark.asyncio
async def test_peek_noop_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', False, raising=False)
    list_pending = AsyncMock(return_value=[_pledge()])
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', list_pending)

    assert await upsell.peek_pending_tip_kopeks(AsyncMock(), user_id=42, credited_amount_kopeks=51000) == 0
    list_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_peek_noop_without_session(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)
    list_pending = AsyncMock(return_value=[_pledge()])
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', list_pending)

    assert await upsell.peek_pending_tip_kopeks(None, user_id=42, credited_amount_kopeks=51000) == 0
    list_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_peek_survives_db_error(monkeypatch):
    """Упавший запрос не имеет права уронить начисление комиссии — тихий ноль."""
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)
    monkeypatch.setattr(
        upsell.pledge_crud, 'list_pending_pledges', AsyncMock(side_effect=RuntimeError('БД недоступна'))
    )

    assert await upsell.peek_pending_tip_kopeks(AsyncMock(), user_id=42, credited_amount_kopeks=51000) == 0


# --- applied_tip_for_credited_amount: разбор уже проведённого платежа --------------


@pytest.mark.asyncio
async def test_applied_tip_found_for_old_deposit(monkeypatch):
    """Транзакция DEPOSIT хранит полную сумму — надбавку надо уметь найти задним числом."""
    monkeypatch.setattr(upsell.pledge_crud, 'list_applied_pledges', AsyncMock(return_value=[_pledge()]))

    tip = await upsell.applied_tip_for_credited_amount(AsyncMock(), user_id=42, credited_amount_kopeks=51000)

    assert tip == 1000


@pytest.mark.asyncio
async def test_applied_tip_zero_for_unrelated_deposit(monkeypatch):
    monkeypatch.setattr(upsell.pledge_crud, 'list_applied_pledges', AsyncMock(return_value=[_pledge()]))

    assert await upsell.applied_tip_for_credited_amount(AsyncMock(), user_id=42, credited_amount_kopeks=30000) == 0


@pytest.mark.asyncio
async def test_applied_tip_ignores_feature_flags(monkeypatch):
    """История не переписывается выключателем: надбавка списана — значит списана."""
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', False, raising=False)
    monkeypatch.setattr(upsell.pledge_crud, 'list_applied_pledges', AsyncMock(return_value=[_pledge()]))

    assert await upsell.applied_tip_for_credited_amount(AsyncMock(), user_id=42, credited_amount_kopeks=51000) == 1000


@pytest.mark.asyncio
async def test_applied_tip_survives_db_error(monkeypatch):
    monkeypatch.setattr(
        upsell.pledge_crud, 'list_applied_pledges', AsyncMock(side_effect=RuntimeError('БД недоступна'))
    )

    assert await upsell.applied_tip_for_credited_amount(AsyncMock(), user_id=42, credited_amount_kopeks=51000) == 0


@pytest.mark.asyncio
async def test_applied_tip_zero_without_session():
    assert await upsell.applied_tip_for_credited_amount(None, user_id=42, credited_amount_kopeks=51000) == 0


@pytest.mark.asyncio
async def test_thank_you_names_both_amounts(monkeypatch):
    """Чек провайдера уже показал 510 ₽ — благодарность обязана объяснить, где 500."""
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', AsyncMock(return_value=[]))
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await upsell._notify_pledge_applied(bot, AsyncMock(), _user(), 1000, 'yookassa', credited_kopeks=50000)

    message = bot.send_message.await_args.kwargs['text']
    assert '10' in message  # ушло на проект
    assert '500' in message  # осталось на балансе


@pytest.mark.asyncio
async def test_apply_passes_credited_amount_to_thank_you(applied_env, monkeypatch):
    """Списанную надбавку и остаток считает сам сервис, а не текст сообщения."""
    notify = AsyncMock()
    monkeypatch.setattr(upsell, '_notify_pledge_applied', notify, raising=False)

    await upsell.try_apply_donation_pledge(AsyncMock(), _user(), credited_amount_kopeks=51000, bot=MagicMock())

    assert notify.await_args.kwargs['credited_kopeks'] == 50000


# --- cancel_pending_pledges -------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_pending_pledges_commits(monkeypatch):
    supersede = AsyncMock(return_value=2)
    monkeypatch.setattr(upsell.pledge_crud, 'supersede_pending_pledges', supersede)
    db = AsyncMock()

    cancelled = await upsell.cancel_pending_pledges(db, _user())

    assert cancelled == 2
    supersede.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_pending_pledges_without_pledges_does_not_commit(monkeypatch):
    monkeypatch.setattr(upsell.pledge_crud, 'supersede_pending_pledges', AsyncMock(return_value=0))
    db = AsyncMock()

    assert await upsell.cancel_pending_pledges(db, _user()) == 0
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_pending_pledges_survives_db_error(monkeypatch):
    """Гашение — предохранитель, а не обязанность: сбой не имеет права ронять оплату."""
    monkeypatch.setattr(
        upsell.pledge_crud, 'supersede_pending_pledges', AsyncMock(side_effect=RuntimeError('БД недоступна'))
    )

    assert await upsell.cancel_pending_pledges(AsyncMock(), _user()) == 0
