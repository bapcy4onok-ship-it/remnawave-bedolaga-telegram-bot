"""Награда за задание на суточном тарифе: перенос ближайшего суточного списания.

Суточную подписку ``process_daily_charges`` списывает, когда с
``last_daily_charge_at`` прошли сутки (``get_daily_subscriptions_for_charge``), —
дата окончания тут ни при чём. Без переноса оживлённую наградой суточную со
старым списанием списали бы сразу после награды, а у живой подаренные дни
пропадали бы. Теперь награда сдвигает ближайшее списание на те же дни, но не
дальше новой даты окончания:
``last_daily_charge_at = min(max(прошлое, now − 24 ч) + дни, новый конец − 24 ч)`` —
в той же транзакции, что и продление, и откатывается вместе с ним.

Оживляется только истёкшая (EXPIRED) суточная. Отключённую (DISABLED) — в
том числе после «Обнулить» и приостановки за нехватку баланса — награда не
трогает вовсе (ответ владельца 2026-10-09), поэтому сценарии переноса списания у
закончившейся подписки проверяются на EXPIRED.

Настоящие сервис заданий, SubscriptionService и crud на in-memory SQLite;
подменены клиент панели, Lava, Telegram и Redis. «Через N дней» — подменённые
часы в ``get_daily_subscriptions_for_charge``.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from sqlalchemy import select, update

from app.config import settings
from app.database.crud import subscription as subscription_crud
from app.database.crud.subscription import get_daily_subscriptions_for_charge, reset_subscription
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
    server_squad_promo_groups,
    tariff_promo_groups,
)
from app.external.remnawave_api import RemnaWaveAPIError
from app.services import reward_task_service as service
from app.services.grace_access_runtime import grace_access_runtime
from app.services.grace_access_service import GraceAccessMode
from app.services.reward_task_service import RewardTaskError
from app.services.subscription_service import SubscriptionService
from app.utils.cache import RateLimitCache
from tests.fixtures.sqlite_memory import memory_session


TABLES = [
    PromoGroup.__table__,
    User.__table__,
    UserPromoGroup.__table__,
    Tariff.__table__,
    tariff_promo_groups,
    Subscription.__table__,
    TrafficPurchase.__table__,
    SentNotification.__table__,
    RewardTask.__table__,
    RewardTaskSubmission.__table__,
    # «Обнулить» стирает сквады, а подписке без сквадов награда ищет их заранее.
    ServerSquad.__table__,
    server_squad_promo_groups,
]

USER_ID = 1
SUBSCRIPTION_ID = 10
SECOND_SUBSCRIPTION_ID = 11
BOOST_TASK_ID = 101
SECOND_BOOST_TASK_ID = 102
DAILY_TARIFF_ID = 7
PERIOD_TARIFF_ID = 8
PANEL_USER_ID = 4242
REWARD_DAYS = 3
DAY = timedelta(hours=24)
REWARD = timedelta(days=REWARD_DAYS)
# Допуск на время между «до награды» и моментом внутри неё.
SLACK = timedelta(seconds=30)


class FakePanelAPI:
    """Клиент Remnawave под настоящим SubscriptionService: пользователь есть, PATCH запоминается."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.updated: list[dict] = []
        self.update_error: Exception | None = None

    @staticmethod
    def _panel_user(panel_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=panel_id,
            telegram_id=555,
            short_uuid='shortuuid',
            subscription_url='https://sub.example/x',
            happ_crypto_link='',
        )

    async def get_user_by_id(self, user_id):
        self.calls.append('get_user_by_id')
        return self._panel_user(user_id)

    async def update_user(self, **kwargs):
        self.calls.append('update_user')
        self.updated.append(kwargs)
        if self.update_error is not None:
            raise self.update_error
        return self._panel_user(kwargs['user_id'])

    async def create_user(self, **kwargs):  # создания в этих сценариях быть не должно
        self.calls.append('create_user')
        raise AssertionError('unexpected create_user')


@pytest.fixture(autouse=True)
def _tasks_for_all_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """Раздел открыт всем: здесь проверяется перенос списания, а не режим «только админы»."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', True)


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch) -> FakePanelAPI:
    api = FakePanelAPI()

    @contextlib.asynccontextmanager
    async def _client(self):
        yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', _client)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(grace_access_runtime, '_mode', GraceAccessMode.DISABLED)
    monkeypatch.setattr(RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False))

    from app.services.payment import lava as lava_module

    monkeypatch.setattr(lava_module, 'shift_lava_next_charge_after_manual_extension', AsyncMock())
    notify_bot = MagicMock()
    notify_bot.send_message = AsyncMock()
    notify_bot.session.close = AsyncMock()
    monkeypatch.setattr(service, 'create_bot', lambda: notify_bot)
    service.invalidate_active_tasks_cache()
    return api


def _subscription(
    subscription_id: int,
    *,
    tariff_id: int,
    status: str,
    end: datetime,
    last_charge: datetime | None,
    paused: bool = False,
) -> Subscription:
    return Subscription(
        id=subscription_id,
        user_id=USER_ID,
        status=status,
        is_trial=False,
        end_date=end,
        tariff_id=tariff_id,
        last_daily_charge_at=last_charge,
        is_daily_paused=paused,
        traffic_limit_gb=0,
        connected_squads=['squad-1'],
        remnawave_short_id=f's{subscription_id}',
        remnawave_id=PANEL_USER_ID if subscription_id == SUBSCRIPTION_ID else None,
    )


@contextlib.asynccontextmanager
async def _world(monkeypatch: pytest.MonkeyPatch, *rows: Subscription | RewardTask):
    """Пользователь с балансом (списывать есть чем), суточный и периодный тарифы, задание на буст.

    ``rows`` — подписки и другие строки (ещё задания) в той же транзакции.
    """
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
                    balance_kopeks=100_000,
                    remnawave_id=PANEL_USER_ID,
                ),
                Tariff(
                    id=DAILY_TARIFF_ID,
                    name='Суточный',
                    is_daily=True,
                    daily_price_kopeks=1000,
                    period_prices={},
                    allowed_squads=['squad-1'],
                ),
                Tariff(
                    id=PERIOD_TARIFF_ID,
                    name='Месяц',
                    period_prices={'30': 10000},
                    allowed_squads=['squad-1'],
                ),
                RewardTask(
                    id=BOOST_TASK_ID,
                    kind='channel_boost',
                    title='Буст канала',
                    reward_days=REWARD_DAYS,
                    channel_id='-1003822976183',
                    min_boosts=1,
                    created_at=now,
                    updated_at=now,
                ),
                *rows,
            ]
        )
        await db.commit()
        yield db


def _daily(**kwargs) -> Subscription:
    return _subscription(SUBSCRIPTION_ID, tariff_id=DAILY_TARIFF_ID, **kwargs)


def _second_boost_task() -> RewardTask:
    """Второе задание: одно и то же дважды не выполнить, а тесту нужна вторая награда."""
    now = datetime.now(UTC)
    return RewardTask(
        id=SECOND_BOOST_TASK_ID,
        kind='channel_boost',
        title='Буст второго канала',
        reward_days=REWARD_DAYS,
        channel_id='-1001234567890',
        min_boosts=1,
        created_at=now,
        updated_at=now,
    )


def _boost_bot() -> MagicMock:
    bot = MagicMock()
    boost = SimpleNamespace(expiration_date=datetime.now(UTC) + timedelta(days=30))
    bot.get_user_chat_boosts = AsyncMock(return_value=SimpleNamespace(boosts=[boost]))
    return bot


async def _boost(db, task_id: int = BOOST_TASK_ID) -> SimpleNamespace:
    """Буст засчитан; возвращает результат и границы момента начисления."""
    user = (await db.execute(select(User).where(User.id == USER_ID))).scalar_one()
    task = await service.require_active_task(db, task_id, 'channel_boost')
    started = datetime.now(UTC)
    result = await service.check_boost(db, user, task, _boost_bot())
    return SimpleNamespace(result=result, started=started, finished=datetime.now(UTC))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _row(db, subscription_id: int = SUBSCRIPTION_ID) -> SimpleNamespace:
    row = (
        await db.execute(
            select(Subscription.status, Subscription.end_date, Subscription.last_daily_charge_at).where(
                Subscription.id == subscription_id
            )
        )
    ).one()
    return SimpleNamespace(status=row.status, end=_aware(row.end_date), last_charge=_aware(row.last_daily_charge_at))


def _clock(moment: datetime) -> type[datetime]:
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

    return _FrozenDatetime


async def _charged_ids(db, monkeypatch: pytest.MonkeyPatch, *, at: datetime | None = None) -> list[int]:
    """Кого спишет ``process_daily_charges`` сейчас (или в момент ``at``)."""
    with monkeypatch.context() as patch:
        if at is not None:
            patch.setattr(subscription_crud, 'datetime', _clock(at))
        return [subscription.id for subscription in await get_daily_subscriptions_for_charge(db)]


# ── Живая суточная ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alive_daily_subscription_gets_its_next_charge_postponed_by_the_reward(monkeypatch, panel):
    """Списали 2 ч назад: следующее списание было бы через 22 ч, теперь — через 22 ч + 3 дня."""
    last = datetime.now(UTC) - timedelta(hours=2)
    async with _world(monkeypatch, _daily(status='active', end=last + DAY, last_charge=last)) as db:
        await _boost(db)

        row = await _row(db)
        assert row.last_charge == last + REWARD
        assert row.end == last + DAY + REWARD
        # Ближайшее списание — ровно в новую дату окончания: подаренные дни бесплатны.
        assert row.last_charge + DAY == row.end
        assert await _charged_ids(db, monkeypatch) == []
        assert await _charged_ids(db, monkeypatch, at=row.end - timedelta(minutes=1)) == []
        assert await _charged_ids(db, monkeypatch, at=row.end + timedelta(minutes=1)) == [SUBSCRIPTION_ID]
        [payload] = panel.updated
        assert _aware(payload['expire_at']) == row.end


@pytest.mark.asyncio
async def test_overdue_daily_charge_counts_from_now(monkeypatch, panel):
    """Списание просрочено (30 ч назад, задание опередило джобу): переносится от «сейчас», а не от прошлого."""
    last = datetime.now(UTC) - timedelta(hours=30)
    async with _world(monkeypatch, _daily(status='active', end=last + DAY, last_charge=last)) as db:
        grant = await _boost(db)

        row = await _row(db)
        assert grant.started - DAY + REWARD <= row.last_charge <= grant.finished - DAY + REWARD
        assert grant.started + REWARD <= row.end <= grant.finished + REWARD
        assert abs(row.last_charge + DAY - row.end) < SLACK
        assert await _charged_ids(db, monkeypatch) == []


# ── Оживление истёкшей суточной ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'end_ago', 'last_ago'),
    [
        # Истекла давно (старше суток — process_auto_resume её уже не восстановит).
        ('expired', timedelta(days=9), timedelta(days=10)),
        # Истекла час назад, последнее списание — сутки с лишним назад. (Здесь до
        # 2026-10-09 была DISABLED, приостановленная за нехватку баланса: такую
        # награда больше не оживляет — см. test_disabled_daily_subscription_is_not_revived.)
        ('expired', timedelta(hours=1), timedelta(hours=25)),
        # Списаний не было вовсе.
        ('expired', timedelta(days=2), None),
    ],
    ids=['expired_old_charge', 'expired_recent_end', 'never_charged'],
)
async def test_revived_daily_subscription_is_not_charged_until_the_reward_days_run_out(
    monkeypatch, panel, status, end_ago, last_ago
):
    """Раньше оживлённую суточную со старым списанием списывали сразу после награды —
    списание, которого без награды не было бы. Теперь — только когда кончатся подаренные дни."""
    now = datetime.now(UTC)
    last = now - last_ago if last_ago is not None else None
    async with _world(monkeypatch, _daily(status=status, end=now - end_ago, last_charge=last)) as db:
        grant = await _boost(db)

        row = await _row(db)
        assert row.status == 'active'
        assert grant.started - DAY + REWARD <= row.last_charge <= grant.finished - DAY + REWARD
        assert grant.started + REWARD <= row.end <= grant.finished + REWARD
        assert await _charged_ids(db, monkeypatch) == []
        assert await _charged_ids(db, monkeypatch, at=grant.started + REWARD - timedelta(hours=1)) == []
        assert await _charged_ids(db, monkeypatch, at=grant.finished + REWARD + timedelta(minutes=1)) == [
            SUBSCRIPTION_ID
        ]


@pytest.mark.asyncio
async def test_paused_daily_subscription_is_postponed_too(monkeypatch, panel):
    """На паузе не списывают и так; после снятия паузы подаренные дни тоже бесплатны."""
    last = datetime.now(UTC) - timedelta(hours=2)
    async with _world(monkeypatch, _daily(status='active', end=last + DAY, last_charge=last, paused=True)) as db:
        await _boost(db)

        row = await _row(db)
        assert row.last_charge == last + REWARD
        assert row.end == last + DAY + REWARD
        await db.execute(update(Subscription).where(Subscription.id == SUBSCRIPTION_ID).values(is_daily_paused=False))
        await db.commit()
        assert await _charged_ids(db, monkeypatch) == []
        assert await _charged_ids(db, monkeypatch, at=row.end - timedelta(minutes=1)) == []
        assert await _charged_ids(db, monkeypatch, at=row.end + timedelta(minutes=1)) == [SUBSCRIPTION_ID]


# ── Не дальше новой даты окончания ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_padded_daily_subscription_is_postponed_by_the_reward_days_only(monkeypatch, panel):
    """Купон или админ добавили дней сверх суток: списание переносится ровно на дни награды,
    а не к новой дате окончания — до неё суточные списания идут, как шли."""
    last = datetime.now(UTC) - timedelta(hours=2)
    end = last + DAY + timedelta(days=5)
    async with _world(monkeypatch, _daily(status='active', end=end, last_charge=last)) as db:
        await _boost(db)

        row = await _row(db)
        assert row.end == end + REWARD
        assert row.last_charge == last + REWARD
        next_charge = row.last_charge + DAY
        assert next_charge < row.end
        assert await _charged_ids(db, monkeypatch, at=next_charge - timedelta(minutes=1)) == []
        assert await _charged_ids(db, monkeypatch, at=next_charge + timedelta(minutes=1)) == [SUBSCRIPTION_ID]


@pytest.mark.asyncio
async def test_stacked_rewards_on_an_alive_daily_subscription_add_up(monkeypatch, panel):
    """Две награды подряд: списание уезжает на сумму их дней — ровно в новую дату окончания."""
    last = datetime.now(UTC) - timedelta(hours=2)
    async with _world(
        monkeypatch, _daily(status='active', end=last + DAY, last_charge=last), _second_boost_task()
    ) as db:
        await _boost(db)
        await _boost(db, SECOND_BOOST_TASK_ID)

        row = await _row(db)
        assert row.last_charge == last + 2 * REWARD
        assert row.end == last + DAY + 2 * REWARD


@pytest.mark.asyncio
async def test_alive_daily_subscription_with_a_shortened_end_is_charged_exactly_at_its_new_end(monkeypatch, panel):
    """Админ сократил срок живой суточной (set_end_date): окончание раньше прошлого списания
    + 24 ч. Перенос на дни награды поставил бы списание позже новой даты окончания — предел
    ставит его ровно в неё, как у закончившихся."""
    last = datetime.now(UTC) - timedelta(hours=2)
    end = last + timedelta(hours=22)
    async with _world(monkeypatch, _daily(status='active', end=end, last_charge=last)) as db:
        await _boost(db)

        row = await _row(db)
        assert row.status == 'active'
        assert row.end == end + REWARD
        assert row.last_charge + DAY == row.end
        [payload] = panel.updated
        assert _aware(payload['expire_at']) == row.end
        assert await _charged_ids(db, monkeypatch, at=row.end - timedelta(minutes=1)) == []
        assert await _charged_ids(db, monkeypatch, at=row.end + timedelta(minutes=1)) == [SUBSCRIPTION_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'last_from_now',
    [
        # Срок кончился через 2 ч после списания (админ сократил его): списание недавнее.
        -timedelta(hours=3),
        # Списание уже унесла вперёд прошлая награда, потом срок кончился.
        timedelta(days=2),
    ],
    ids=['recent_charge', 'charge_already_in_the_future'],
)
async def test_lapsed_daily_subscription_is_charged_exactly_at_its_new_end(monkeypatch, panel, last_from_now):
    """Перенос от прошлого списания поставил бы ближайшее списание позже новой даты окончания:
    на ~21 ч, а при уже перенесённом — на дни. В дату окончания панель выключает пользователя,
    а строка остаётся ACTIVE и не списывается (активные суточные monitoring не экспайрит):
    доступа нет, пока не дойдёт списание. Теперь списание ровно в новую дату окончания.

    До 2026-10-09 здесь была DISABLED после «Обнулить»; её награда больше не оживляет, предел
    проверяется на истёкшей строке с тем же недавним (или унесённым вперёд) списанием."""
    now = datetime.now(UTC)
    ended_at = now - timedelta(hours=1)
    async with _world(monkeypatch, _daily(status='expired', end=ended_at, last_charge=now + last_from_now)) as db:
        with structlog.testing.capture_logs() as events:
            grant = await _boost(db)

        row = await _row(db)
        assert row.status == 'active'
        assert grant.started + REWARD <= row.end <= grant.finished + REWARD
        assert row.last_charge + DAY == row.end
        [payload] = panel.updated
        assert _aware(payload['expire_at']) == row.end
        assert await _charged_ids(db, monkeypatch, at=row.end - timedelta(minutes=1)) == []
        assert await _charged_ids(db, monkeypatch, at=row.end + timedelta(minutes=1)) == [SUBSCRIPTION_ID]
        [record] = [event for event in events if event['event'] == '🎁 Начислена награда за задание']
        assert record['next_daily_charge_at'] == record['end_date'] == row.end


@pytest.mark.asyncio
@pytest.mark.parametrize('rewarded_before_reset', [False, True], ids=['reset_after_a_charge', 'reset_after_a_reward'])
async def test_reset_daily_subscription_is_not_revived(monkeypatch, panel, rewarded_before_reset):
    """Настоящий «Обнулить» (reset_subscription): DISABLED, срок = сейчас, сквады стёрты.

    До 2026-10-09 награда её оживляла (и переносила списание); теперь раздела у
    пользователя нет: буст — ``tasks_unavailable``, строка и панель не тронуты.
    """
    last = datetime.now(UTC) - timedelta(hours=3)
    async with _world(
        monkeypatch, _daily(status='active', end=last + DAY, last_charge=last), _second_boost_task()
    ) as db:
        if rewarded_before_reset:
            await _boost(db, SECOND_BOOST_TASK_ID)
        await reset_subscription(db, await db.get(Subscription, SUBSCRIPTION_ID))
        reset = await _row(db)
        assert reset.status == 'disabled'
        updates_before = len(panel.updated)

        with pytest.raises(RewardTaskError) as error:
            await _boost(db)

        assert error.value.code == 'tasks_unavailable'
        assert await _row(db) == reset
        assert len(panel.updated) == updates_before
        approved = (await db.execute(select(RewardTaskSubmission.task_id))).scalars().all()
        assert approved == ([SECOND_BOOST_TASK_ID] if rewarded_before_reset else [])


@pytest.mark.asyncio
async def test_disabled_daily_subscription_is_not_revived(monkeypatch, panel):
    """Суточная, приостановленная за нехватку баланса (DISABLED, срок вышел): награда её не трогает."""
    now = datetime.now(UTC)
    async with _world(
        monkeypatch, _daily(status='disabled', end=now - timedelta(hours=1), last_charge=now - timedelta(hours=25))
    ) as db:
        before = await _row(db)

        with pytest.raises(RewardTaskError) as error:
            await _boost(db)

        assert error.value.code == 'tasks_unavailable'
        assert await _row(db) == before
        assert panel.updated == []
        assert (await db.execute(select(RewardTaskSubmission.id))).all() == []


# ── Что не трогается ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize('last_ago', [None, timedelta(days=3)], ids=['no_charge_time', 'stale_charge_time'])
async def test_non_daily_subscription_keeps_its_charge_time(monkeypatch, panel, last_ago):
    """Несуточный тариф: поле суточного списания не трогаем (оно могло остаться от смены тарифа)."""
    now = datetime.now(UTC)
    last = now - last_ago if last_ago is not None else None
    subscription = _subscription(
        SUBSCRIPTION_ID, tariff_id=PERIOD_TARIFF_ID, status='active', end=now + timedelta(days=5), last_charge=last
    )
    async with _world(monkeypatch, subscription) as db:
        await _boost(db)

        row = await _row(db)
        assert row.last_charge == last
        assert row.end == _aware(subscription.end_date)


@pytest.mark.asyncio
async def test_daily_subscription_next_to_a_periodic_one_is_not_touched(monkeypatch, panel):
    """Живая несуточная важнее суточной: награда достаётся ей, суточная как была."""
    now = datetime.now(UTC)
    last = now - timedelta(hours=2)
    periodic = _subscription(
        SECOND_SUBSCRIPTION_ID,
        tariff_id=PERIOD_TARIFF_ID,
        status='active',
        end=now + timedelta(days=5),
        last_charge=None,
    )
    async with _world(monkeypatch, _daily(status='active', end=last + DAY, last_charge=last), periodic) as db:
        grant = await _boost(db)

        assert grant.result.subscription_id == SECOND_SUBSCRIPTION_ID
        daily = await _row(db)
        assert (daily.last_charge, daily.end) == (last, last + DAY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'end_ago', 'last_ago'),
    [
        ('active', timedelta(hours=-22), timedelta(hours=2)),
        ('expired', timedelta(days=9), timedelta(days=10)),
    ],
    ids=['alive', 'expired'],
)
async def test_failed_sync_leaves_the_charge_time_unchanged(monkeypatch, panel, status, end_ago, last_ago):
    """Панель не приняла продление: откат — и дни, и перенос списания, ничего не начислено."""
    now = datetime.now(UTC)
    last = now - last_ago
    async with _world(monkeypatch, _daily(status=status, end=now - end_ago, last_charge=last)) as db:
        before = await _row(db)
        panel.update_error = RemnaWaveAPIError('Internal server error', 500)

        with pytest.raises(RewardTaskError) as error:
            await _boost(db)

        assert error.value.code == 'panel_sync_failed'
        assert await _row(db) == before
        assert before.last_charge == last
        submissions = (await db.execute(select(RewardTaskSubmission.id))).all()
        assert submissions == []


# ── Экран подписки в боте: «До списания» дальше суток ────────────────────────


@pytest.mark.parametrize(
    ('until', 'text'),
    [
        (timedelta(hours=5, minutes=7, seconds=30), '5ч 7мин'),
        (timedelta(minutes=59), '0ч 59мин'),
        (timedelta(hours=24), '1д 0ч 0мин'),
        (timedelta(days=3, hours=22, minutes=5), '3д 22ч 5мин'),
    ],
)
def test_daily_countdown_counts_whole_days(until, text):
    from app.handlers.subscription.purchase import _format_daily_countdown

    assert _format_daily_countdown(until) == text


@pytest.mark.asyncio
@pytest.mark.parametrize(('paused', 'label'), [(False, '⏳ До списания: '), (True, '⏳ Осталось: ')])
async def test_subscription_screen_shows_days_until_a_postponed_charge(monkeypatch, paused, label):
    """Награда перенесла списание на 3 дня вперёд. Раньше экран брал ``timedelta.seconds``
    (остаток без целых дней) и показывал «0ч 59мин» вместо «3д 0ч 59мин»."""
    from app.database.crud import tariff as tariff_crud
    from app.handlers.subscription import purchase

    now = datetime.now(UTC)
    tariff = SimpleNamespace(
        id=DAILY_TARIFF_ID, name='Суточный', is_daily=True, traffic_limit_gb=0, device_limit=1, daily_price_kopeks=1000
    )
    subscription = SimpleNamespace(
        id=SUBSCRIPTION_ID,
        status='active',
        is_trial=False,
        end_date=now + timedelta(days=3, hours=1),
        tariff_id=DAILY_TARIFF_ID,
        tariff=None,
        last_daily_charge_at=now + timedelta(days=2, hours=1),  # списание через 3 дня 1 час
        is_daily_paused=paused,
        traffic_used_gb=0.0,
        traffic_limit_gb=0,
        connected_squads=['squad-1'],
        device_limit=1,
    )
    db_user = SimpleNamespace(
        id=USER_ID,
        subscription=subscription,
        language='ru',
        full_name='Alice',
        telegram_id=555,
        balance_kopeks=0,
        remnawave_id=None,
        promo_offer_discount_percent=0,
        promo_offer_discount_expires_at=None,
        get_primary_promo_group=lambda: None,
    )
    db = MagicMock()
    db.refresh = AsyncMock()
    callback = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()

    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', False)
    monkeypatch.setattr(subscription_crud, 'check_and_update_subscription_status', AsyncMock(return_value=subscription))
    monkeypatch.setattr(SubscriptionService, 'sync_subscription_usage', AsyncMock(return_value=True))
    monkeypatch.setattr(SubscriptionService, 'ensure_subscription_synced', AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(tariff_crud, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(purchase, 'get_servers_display_names', AsyncMock(return_value='🇳🇱 Нидерланды'))
    monkeypatch.setattr(purchase, 'get_display_subscription_link', lambda subscription: None)
    monkeypatch.setattr(purchase, 'get_subscription_keyboard', MagicMock(return_value=None))

    await purchase.show_subscription_info(callback, db_user, db)

    text = callback.message.edit_text.await_args.args[0]
    assert f'{label}3д 0ч 59мин' in text
