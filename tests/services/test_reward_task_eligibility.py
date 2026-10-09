"""Кому награда не начисляется: заблокированным и тем, у кого подписка отключена.

- Отчёт отправлен, потом пользователя заблокировали (настоящий
  ``UserService.block_user``): одобрение не должно вернуть ему доступ к VPN —
  ``user_blocked``, подписка и панель не тронуты, отчёт остаётся на проверке.
- DISABLED награда не оживляет никогда, какой бы ни была дата окончания (ответ
  владельца 2026-10-09: «скрыть функцию для отключенных подписок или забаненных
  людей»): «Деактивировать», «Обнулить», «Принудительная очистка», триал,
  выключенный за отписку от канала, синк из панели. Раздел такому пользователю
  не показывается (``tasks_unavailable``), а одобрение его отчёта отвечает
  ``subscription_disabled`` и оставляет отчёт на проверке. До 2026-10-09
  DISABLED с вышедшим сроком оживала, как EXPIRED; эти тесты теперь проверяют
  обратное. EXPIRED по-прежнему продлевается и включается (решение 2026-10-08).

Настоящая сессия (in-memory SQLite) и настоящие crud/сервисы; фейки — только
панель (запись вызовов или клиент API под настоящим SubscriptionService), Lava,
Telegram и Redis.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.subscription import deactivate_subscription, reset_subscription
from app.database.models import (
    PromoGroup,
    RewardTask,
    RewardTaskSubmission,
    SentNotification,
    ServerSquad,
    Subscription,
    Tariff,
    TrafficPurchase,
    User,
    UserPromoGroup,
)
from app.external.remnawave_api import UserStatus as PanelUserStatus
from app.services import reward_task_service as service
from app.services.grace_access_runtime import grace_access_runtime
from app.services.grace_access_service import GraceAccessMode
from app.services.remnawave_service import RemnaWaveService
from app.services.reward_task_service import RewardTaskError
from app.services.subscription_service import SubscriptionService
from app.services.user_service import UserService
from app.utils.cache import RateLimitCache
from tests.fixtures.sqlite_memory import memory_session


TABLES = [
    PromoGroup.__table__,
    User.__table__,
    UserPromoGroup.__table__,
    Tariff.__table__,
    Subscription.__table__,
    TrafficPurchase.__table__,
    SentNotification.__table__,
    RewardTask.__table__,
    RewardTaskSubmission.__table__,
    # Подписке без сквадов награда ищет сквады заранее (на случай создания в панели).
    ServerSquad.__table__,
]

USER_ID = 1
ADMIN_ID = 2
SUBSCRIPTION_ID = 10
PHOTO_TASK_ID = 100
BOOST_TASK_ID = 101
FILE_A = 'AgACAgIAAxkBAAIBQ2ZxAAAAAAAAAAAAAAAAAAAA'

DISABLED_TEXT = (
    'Ваша подписка сейчас отключена. Задания станут доступны, когда она снова будет активна. '
    'Если это ошибка — напишите в поддержку.'
)


@pytest.fixture(autouse=True)
def _tasks_for_all_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """Раздел открыт всем: здесь проверяются правила подписок, а не режим «только админы»."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', True)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Панель записывает вызовы (и коммитит, как настоящая), уведомления — мок."""
    state = SimpleNamespace(panel_calls=[], panel_disabled=[])

    def _fake_sync(kind: str):
        async def _impl(self, db, subscription, **kwargs):
            state.panel_calls.append((kind, subscription.id))
            await db.commit()
            return SimpleNamespace(id=4242)

        return _impl

    async def _fake_disable(self, panel_user_id, db=None):
        state.panel_disabled.append(panel_user_id)
        return True

    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', _fake_sync('update'))
    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', _fake_sync('create'))

    @contextlib.asynccontextmanager
    async def _panel_client(self):
        # Перед обновлением сервис спрашивает панель, жив ли её пользователь (на 404 — создание).
        yield SimpleNamespace(get_user_by_id=AsyncMock(side_effect=lambda panel_id: SimpleNamespace(id=panel_id)))

    monkeypatch.setattr(SubscriptionService, 'get_api_client', _panel_client)
    monkeypatch.setattr(SubscriptionService, 'disable_remnawave_user', _fake_disable)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False))

    from app.services.payment import lava as lava_module

    monkeypatch.setattr(lava_module, 'shift_lava_next_charge_after_manual_extension', AsyncMock())
    state.notify_bot = MagicMock()
    state.notify_bot.send_message = AsyncMock()
    state.notify_bot.session.close = AsyncMock()
    monkeypatch.setattr(service, 'create_bot', lambda: state.notify_bot)
    service.invalidate_active_tasks_cache()
    return state


_REAL_UPDATE_REMNAWAVE_USER = SubscriptionService.update_remnawave_user
_REAL_CREATE_REMNAWAVE_USER = SubscriptionService.create_remnawave_user


class _RecordingPanel:
    """Клиент Remnawave под настоящим SubscriptionService: пользователь есть, PATCH запоминается."""

    def __init__(self) -> None:
        self.lookups: list[int] = []
        self.updates: list[dict] = []

    def _user(self, panel_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=panel_id,
            telegram_id=555,
            short_uuid='short',
            subscription_url='https://sub.example/x',
            happ_crypto_link='',
        )

    async def get_user_by_id(self, user_id):
        self.lookups.append(user_id)
        return self._user(user_id)

    async def update_user(self, **kwargs):
        self.updates.append(kwargs)
        return self._user(kwargs['user_id'])


@pytest.fixture
def real_panel(monkeypatch: pytest.MonkeyPatch, env: SimpleNamespace) -> _RecordingPanel:
    """Настоящие update/create_remnawave_user поверх фейкового клиента — виден payload в панель."""
    api = _RecordingPanel()

    @contextlib.asynccontextmanager
    async def _client(self):
        yield api

    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', _REAL_UPDATE_REMNAWAVE_USER)
    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', _REAL_CREATE_REMNAWAVE_USER)
    monkeypatch.setattr(SubscriptionService, 'get_api_client', _client)
    monkeypatch.setattr(grace_access_runtime, '_mode', GraceAccessMode.DISABLED)
    return api


@contextlib.asynccontextmanager
async def _db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subscriptions: tuple[dict, ...] = ({'status': 'active', 'is_trial': False, 'days': 20},),
):
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        db.add_all(
            [
                User(
                    id=USER_ID,
                    telegram_id=555,
                    username='alice',
                    first_name='Alice',
                    language='ru',
                    status='active',
                    remnawave_id=4242,
                ),
                User(id=ADMIN_ID, telegram_id=999, first_name='Admin', language='ru', status='active'),
                RewardTask(
                    id=PHOTO_TASK_ID,
                    kind='photo_report',
                    title='Лайки',
                    reward_days=1,
                    max_photos=3,
                    created_at=now,
                    updated_at=now,
                ),
                RewardTask(
                    id=BOOST_TASK_ID,
                    kind='channel_boost',
                    title='Буст канала',
                    reward_days=3,
                    channel_id='-1003822976183',
                    min_boosts=1,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        for offset, spec in enumerate(subscriptions):
            db.add(
                Subscription(
                    id=SUBSCRIPTION_ID + offset,
                    user_id=USER_ID,
                    status=spec['status'],
                    is_trial=spec.get('is_trial', False),
                    end_date=now + timedelta(days=spec['days']),
                    traffic_limit_gb=0,
                    connected_squads=['squad-1'],
                    remnawave_short_id=f's{offset}',
                    remnawave_id=spec.get('remnawave_id'),
                )
            )
        await db.commit()
        yield db


async def _load_user(db) -> User:
    result = await db.execute(
        select(User)
        .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
        .where(User.id == USER_ID)
    )
    return result.scalar_one()


async def _subscriptions(db) -> dict[int, tuple[str, datetime]]:
    result = await db.execute(select(Subscription.id, Subscription.status, Subscription.end_date))
    return {row.id: (row.status, row.end_date.replace(tzinfo=UTC)) for row in result.all()}


async def _submission_statuses(db) -> list[tuple[int, str]]:
    result = await db.execute(
        select(RewardTaskSubmission.task_id, RewardTaskSubmission.status).order_by(RewardTaskSubmission.id)
    )
    return [tuple(row) for row in result.all()]


async def _submit(db) -> int:
    user = await _load_user(db)
    task = await db.get(RewardTask, PHOTO_TASK_ID)
    return (await service.submit_photo_report(db, user, task, [{'file_id': FILE_A}])).id


async def _pending_report(db) -> int:
    """Отчёт, отправленный раньше (ещё с живой подпиской), — прямо в базу, мимо проверок отправки."""
    submission = RewardTaskSubmission(
        task_id=PHOTO_TASK_ID,
        user_id=USER_ID,
        status='pending',
        source='bot',
        media_items=[{'type': 'photo', 'file_id': FILE_A, 'file_unique_id': None, 'thumb_file_id': None}],
        created_at=datetime.now(UTC),
    )
    db.add(submission)
    await db.commit()
    return submission.id


async def _move_end_date(db, end_date: datetime, subscription_id: int = SUBSCRIPTION_ID) -> None:
    """Прошло время: срок подписки уже такой (``end_date`` в прошлом — срок вышел)."""
    await db.execute(update(Subscription).where(Subscription.id == subscription_id).values(end_date=end_date))
    await db.commit()


def _boost_bot() -> MagicMock:
    bot = MagicMock()
    boost = SimpleNamespace(expiration_date=datetime.now(UTC) + timedelta(days=30))
    bot.get_user_chat_boosts = AsyncMock(return_value=SimpleNamespace(boosts=[boost]))
    return bot


# ── Заблокированный пользователь ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'subscription',
    [
        # Триал: блокировка отключает подписку и пользователя панели.
        {'status': 'active', 'is_trial': True, 'days': 2},
        # Оплаченная: блокировка намеренно оставляет VPN, но бесплатные дни — нет.
        {'status': 'active', 'is_trial': False, 'days': 20},
    ],
    ids=['trial', 'paid'],
)
async def test_report_of_user_blocked_after_submit_is_not_approved(monkeypatch, env, subscription):
    async with _db(monkeypatch, subscriptions=(subscription,)) as db:
        submission_id = await _submit(db)

        assert await UserService().block_user(db, USER_ID, admin_id=ADMIN_ID) is True
        after_block = await _subscriptions(db)
        assert (await db.execute(select(User.status).where(User.id == USER_ID))).scalar_one() == 'blocked'
        admin = await db.get(User, ADMIN_ID)

        results = await service.approve(db, [submission_id], admin)

        assert [(r.id, r.ok, r.error_code) for r in results] == [(submission_id, False, 'user_blocked')]
        assert await _subscriptions(db) == after_block  # подписка не оживлена и не продлена
        assert env.panel_calls == []  # в панель ничего не ушло
        assert await _submission_statuses(db) == [(PHOTO_TASK_ID, 'pending')]
        env.notify_bot.send_message.assert_not_awaited()

        # Отчёт остался на проверке — админ его отклоняет.
        rejected = await service.reject(db, [submission_id], admin, 'Аккаунт заблокирован')
        assert [(r.ok, r.error_code) for r in rejected] == [(True, None)]
        assert await _submission_statuses(db) == [(PHOTO_TASK_ID, 'rejected')]
        env.notify_bot.send_message.assert_not_awaited()  # заблокированным не пишем


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['blocked', 'deleted'])
async def test_inactive_user_gets_no_boost_reward(monkeypatch, env, status):
    """Пользователя заблокировали, пока шла проверка буста: начисление под блокировкой отказывает.

    Объект пользователя хендлер загрузил ещё активным (мидлварь, затем блокировка в
    другой сессии), поэтому видимость раздела проверку прошла — отказ даёт перепроверка
    под блокировкой строки.
    """
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
        before = await _subscriptions(db)
        blocked = update(User).where(User.id == USER_ID).values(status=status)
        await db.execute(blocked.execution_options(synchronize_session=False))  # «другая сессия»
        await db.commit()
        assert user.status == 'active'

        with pytest.raises(RewardTaskError) as error:
            await service.check_boost(db, user, task, _boost_bot())

        assert error.value.code == 'user_blocked'
        assert await _subscriptions(db) == before
        assert await _submission_statuses(db) == []
        assert env.panel_calls == []


@pytest.mark.asyncio
async def test_blocked_user_with_lapsed_disabled_subscription_stays_user_blocked(monkeypatch, env):
    """Блокировка выключила триал, потом его срок вышел: одобрение — ``user_blocked``.

    ``user_blocked`` проверяется под блокировкой раньше выбора подписки. Сам
    пользователь раздела не видит (заблокирован), буст — ``tasks_unavailable``.
    """
    trial = ({'status': 'active', 'is_trial': True, 'days': 2},)
    async with _db(monkeypatch, subscriptions=trial) as db:
        submission_id = await _submit(db)
        assert await UserService().block_user(db, USER_ID, admin_id=ADMIN_ID) is True
        await _move_end_date(db, datetime.now(UTC) - timedelta(days=1))
        before = await _subscriptions(db)
        status, end = before[SUBSCRIPTION_ID]
        assert status == 'disabled' and end < datetime.now(UTC)

        results = await service.approve(db, [submission_id], await db.get(User, ADMIN_ID))
        assert [(r.ok, r.error_code) for r in results] == [(False, 'user_blocked')]

        user = await _load_user(db)
        access = await service.get_reward_tasks_access(db, user)
        assert (access.visible, access.hidden_reason) == (False, 'user_blocked')
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
        bot = _boost_bot()
        with pytest.raises(RewardTaskError) as error:
            await service.check_boost(db, user, task, bot)
        assert error.value.code == 'tasks_unavailable'
        bot.get_user_chat_boosts.assert_not_awaited()

        assert await _subscriptions(db) == before
        assert await _submission_statuses(db) == [(PHOTO_TASK_ID, 'pending')]
        assert env.panel_calls == []


# ── Отключённая подписка: раздел скрыт, награды нет ─────────────────────────


async def _assert_hidden_and_not_revived(db, env, submission_id: int) -> None:
    """Подписка отключена: раздела нет, буст не проверяется, одобрение — ``subscription_disabled``.

    Отчёт остаётся на проверке, подписка и панель не тронуты.
    """
    before = await _subscriptions(db)
    assert all(status == 'disabled' for status, _ in before.values()), before
    user = await _load_user(db)
    assert await service.get_ineligible_reason(db, user) == 'subscription_disabled'
    access = await service.get_reward_tasks_access(db, user)
    assert (access.visible, access.ineligible_reason, access.hidden_reason) == (False, None, 'subscription_disabled')
    with pytest.raises(RewardTaskError) as error:
        await service.list_tasks_for_user(db, user)
    assert error.value.code == 'tasks_unavailable'

    bot = _boost_bot()
    task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
    with pytest.raises(RewardTaskError) as error:
        await service.check_boost(db, user, task, bot)
    assert error.value.code == 'tasks_unavailable'
    bot.get_user_chat_boosts.assert_not_awaited()  # до Telegram не дошли

    # Отчёт, отправленный ещё с живой подпиской: одобрение перепроверяет под блокировкой.
    results = await service.approve(db, [submission_id], await db.get(User, ADMIN_ID))
    assert [(r.ok, r.error_code) for r in results] == [(False, 'subscription_disabled')]

    assert await _subscriptions(db) == before
    assert await _submission_statuses(db) == [(PHOTO_TASK_ID, 'pending')]
    assert env.panel_calls == []
    env.notify_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_deactivated_subscription_with_time_left_hides_the_section(monkeypatch, env):
    """«Деактивировать» оставляет срок: раздела нет, отчёт не принимается, одобрение отказывает."""
    async with _db(monkeypatch) as db:
        await deactivate_subscription(db, await db.get(Subscription, SUBSCRIPTION_ID))
        status, end = (await _subscriptions(db))[SUBSCRIPTION_ID]
        assert status == 'disabled' and end > datetime.now(UTC)

        # Новый отчёт не принимается: раздела для пользователя нет.
        with pytest.raises(RewardTaskError) as error:
            await _submit(db)
        assert error.value.code == 'tasks_unavailable'
        assert await _submission_statuses(db) == []

        # Отчёт, отправленный ещё до деактивации, — прямо в базу.
        await _assert_hidden_and_not_revived(db, env, await _pending_report(db))
        # Код остался для админки: текст одобрения, которое не прошло.
        assert service.format_error_message('subscription_disabled', 'ru') == DISABLED_TEXT


@pytest.mark.asyncio
async def test_subscription_disabled_while_report_waits_keeps_it_pending(monkeypatch, env):
    """Отчёт отправлен с живой подпиской, потом её отключили: одобрение перепроверяет под блокировкой."""
    async with _db(monkeypatch) as db:
        submission_id = await _submit(db)
        await deactivate_subscription(db, await db.get(Subscription, SUBSCRIPTION_ID))
        before = await _subscriptions(db)

        results = await service.approve(db, [submission_id], await db.get(User, ADMIN_ID))

        assert [(r.ok, r.error_code) for r in results] == [(False, 'subscription_disabled')]
        assert await _subscriptions(db) == before
        assert await _submission_statuses(db) == [(PHOTO_TASK_ID, 'pending')]
        assert env.panel_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('days', [7, -7], ids=['time_left', 'lapsed'])
async def test_disabled_and_pending_rows(monkeypatch, env, days):
    """Черновик не в счёт; отключённая — subscription_disabled с любой датой окончания (до 2026-10-09
    отключённая с вышедшим сроком считалась закончившейся и оживала)."""
    subscriptions = ({'status': 'disabled', 'days': days}, {'status': 'pending', 'days': 30})
    async with _db(monkeypatch, subscriptions=subscriptions) as db:
        user = await _load_user(db)
        assert await service.get_ineligible_reason(db, user) == 'subscription_disabled'
        assert (await service.get_reward_tasks_access(db, user)).visible is False


@pytest.mark.asyncio
@pytest.mark.parametrize('days', [7, -7], ids=['time_left', 'lapsed'])
async def test_restricted_user_with_a_disabled_subscription_is_hidden(monkeypatch, env, days):
    """Ограничение от админа объясняется, только пока подписка не отключена: «Отключена» — раздела нет.

    Админка проверяет ограничение под блокировкой первым: одобрение — ``restricted``.
    """
    async with _db(monkeypatch, subscriptions=({'status': 'disabled', 'days': days},)) as db:
        await db.execute(update(User).where(User.id == USER_ID).values(restriction_subscription=True))
        await db.commit()
        user = await _load_user(db)
        assert await service.get_ineligible_reason(db, user) == 'subscription_disabled'
        access = await service.get_reward_tasks_access(db, user)
        assert (access.visible, access.hidden_reason) == (False, 'subscription_disabled')

        submission_id = await _pending_report(db)
        results = await service.approve(db, [submission_id], await db.get(User, ADMIN_ID))
        assert [(r.ok, r.error_code) for r in results] == [(False, 'restricted')]
        assert env.panel_calls == []


@pytest.mark.asyncio
async def test_restricted_user_with_an_alive_subscription_sees_why(monkeypatch, env):
    async with _db(monkeypatch) as db:
        await db.execute(update(User).where(User.id == USER_ID).values(restriction_subscription=True))
        await db.commit()
        user = await _load_user(db)
        assert await service.get_ineligible_reason(db, user) == 'restricted'
        overview = await service.list_tasks_for_user(db, user)
        assert (overview.eligible, overview.ineligible_reason) == (False, 'restricted')


@pytest.mark.asyncio
async def test_expired_sibling_is_revived_next_to_disabled_subscription(monkeypatch, env):
    """Мультитариф: отключённая с остатком срока не мешает оживить соседнюю истёкшую."""
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    disabled_id, expired_id = SUBSCRIPTION_ID, SUBSCRIPTION_ID + 1
    subscriptions = (
        {'status': 'disabled', 'days': 20, 'remnawave_id': 5001},
        {'status': 'expired', 'days': -3, 'remnawave_id': 5002},
    )
    async with _db(monkeypatch, subscriptions=subscriptions) as db:
        before = await _subscriptions(db)
        user = await _load_user(db)
        assert await service.get_ineligible_reason(db, user) is None
        assert (await service.get_reward_tasks_access(db, user)).eligible is True

        submission_id = await _submit(db)
        results = await service.approve(db, [submission_id], await db.get(User, ADMIN_ID))

        assert [(r.ok, r.error_code) for r in results] == [(True, None)]
        after = await _subscriptions(db)
        assert after[disabled_id] == before[disabled_id]  # отключённая не тронута
        status, end = after[expired_id]
        assert status == 'active'
        assert timedelta(hours=23) < end - datetime.now(UTC) <= timedelta(days=1)
        assert env.panel_calls == [('update', expired_id)]
        stored = (await db.execute(select(RewardTaskSubmission.subscription_id))).scalar_one()
        assert stored == expired_id


@pytest.mark.asyncio
async def test_older_expired_sibling_is_revived_not_the_newer_disabled_one(monkeypatch, env):
    """Мультитариф: отключённая кончилась позже истёкшей, но оживает истёкшая.

    До 2026-10-09 из закончившихся оживала та, что кончилась последней, — DISABLED тоже.
    """
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    disabled_id, expired_id = SUBSCRIPTION_ID, SUBSCRIPTION_ID + 1
    subscriptions = (
        {'status': 'disabled', 'days': -2, 'remnawave_id': 5001},
        {'status': 'expired', 'days': -10, 'remnawave_id': 5002},
    )
    async with _db(monkeypatch, subscriptions=subscriptions) as db:
        before = await _subscriptions(db)
        submission_id = await _submit(db)
        results = await service.approve(db, [submission_id], await db.get(User, ADMIN_ID))

        assert [(r.ok, r.error_code) for r in results] == [(True, None)]
        after = await _subscriptions(db)
        assert after[disabled_id] == before[disabled_id]  # отключённая не тронута
        status, end = after[expired_id]
        assert status == 'active'
        assert timedelta(hours=23) < end - datetime.now(UTC) <= timedelta(days=1)
        assert env.panel_calls == [('update', expired_id)]
        stored = (await db.execute(select(RewardTaskSubmission.subscription_id))).scalar_one()
        assert stored == expired_id


# ── Отключённая с вышедшим сроком: раньше оживала, теперь нет (2026-10-09) ──


@pytest.mark.asyncio
async def test_reset_subscription_is_not_revived(monkeypatch, env):
    """«Обнулить» ставит DISABLED и срок = сейчас: награда её не оживляет, раздела нет.

    Отчёт отправлен до «Обнулить»: одобрение отвечает ``subscription_disabled``.
    """
    async with _db(monkeypatch) as db:
        submission_id = await _submit(db)
        await reset_subscription(db, await db.get(Subscription, SUBSCRIPTION_ID))
        status, end = (await _subscriptions(db))[SUBSCRIPTION_ID]
        assert status == 'disabled' and end <= datetime.now(UTC)

        await _assert_hidden_and_not_revived(db, env, submission_id)


@pytest.mark.asyncio
async def test_expired_trial_is_revived(monkeypatch, env, real_panel):
    """Истёкший триал оживает наградой: тот же PATCH в панель, что у продления; триал платным не стал."""
    trial = ({'status': 'active', 'is_trial': True, 'days': 3},)
    async with _db(monkeypatch, subscriptions=trial) as db:
        subscription = await db.get(Subscription, SUBSCRIPTION_ID)
        subscription.status = 'expired'
        await db.commit()
        await _move_end_date(db, datetime.now(UTC) - timedelta(days=4))

        user = await _load_user(db)
        assert await service.get_ineligible_reason(db, user) is None
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
        result = await service.check_boost(db, user, task, _boost_bot())

        row = (
            await db.execute(
                select(Subscription.status, Subscription.is_trial, Subscription.end_date).where(
                    Subscription.id == SUBSCRIPTION_ID
                )
            )
        ).one()
        end = row.end_date if row.end_date.tzinfo else row.end_date.replace(tzinfo=UTC)
        assert (row.status, row.is_trial) == ('active', True)
        assert timedelta(days=2, hours=23) < end - datetime.now(UTC) <= timedelta(days=3)
        assert result.reward_days == 3 and result.subscription_id == SUBSCRIPTION_ID
        assert real_panel.lookups == [4242]
        [payload] = real_panel.updates
        assert payload['user_id'] == 4242
        assert payload['status'] == PanelUserStatus.ACTIVE
        expire_at = payload['expire_at']
        assert (expire_at if expire_at.tzinfo else expire_at.replace(tzinfo=UTC)) == end
        assert await _submission_statuses(db) == [(BOOST_TASK_ID, 'approved')]


@pytest.mark.asyncio
async def test_trial_disabled_for_leaving_the_channel_is_not_revived(monkeypatch, env, real_panel):
    """Прод: CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE. Триал выключили за отписку от канала
    (deactivate_subscription, срок сохранён), пользователь вернулся, когда срок уже вышел:
    строка осталась DISABLED. До 2026-10-09 награда оживляла её как истёкший триал,
    теперь — нет: подписка «Отключена», раздела нет."""
    trial = ({'status': 'active', 'is_trial': True, 'days': 3},)
    async with _db(monkeypatch, subscriptions=trial) as db:
        submission_id = await _submit(db)
        await deactivate_subscription(db, await db.get(Subscription, SUBSCRIPTION_ID))  # как monitoring_service
        await _move_end_date(db, datetime.now(UTC) - timedelta(days=4))

        await _assert_hidden_and_not_revived(db, env, submission_id)
        assert real_panel.lookups == []
        assert real_panel.updates == []


@pytest.mark.asyncio
async def test_subscription_disabled_by_panel_sync_after_its_end_is_not_revived(monkeypatch, env):
    """Синк из панели переносит панельный DISABLED в базу раньше проверки срока (закрытие
    grace выключает истёкших в панели): истёкшая подписка становится DISABLED. До
    2026-10-09 награда оживляла её, как истёкшую; теперь раздела нет.
    """
    async with _db(monkeypatch, subscriptions=({'status': 'expired', 'days': -10},)) as db:
        submission_id = await _submit(db)  # пока подписка истёкшая — отчёт принимается
        panel_user = {
            'id': 4242,
            'status': 'DISABLED',
            'expireAt': (datetime.now(UTC) - timedelta(days=10)).isoformat().replace('+00:00', 'Z'),
            'activeInternalSquads': [],
        }
        await RemnaWaveService()._update_subscription_from_panel_data(
            db, await _load_user(db), panel_user, open_grace_ids=set()
        )
        await db.commit()
        status, end = (await _subscriptions(db))[SUBSCRIPTION_ID]
        assert status == 'disabled' and end < datetime.now(UTC)

        await _assert_hidden_and_not_revived(db, env, submission_id)
