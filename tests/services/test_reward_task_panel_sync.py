"""Синхронизация награды с панелью — на НАСТОЯЩЕМ SubscriptionService.

update_remnawave_user и create_remnawave_user на таймауте (CancelledError внутри)
и на 404 → пересоздании откатывают сессию изнутри. Откат делает ORM-объекты
истёкшими, и раньше сервис заданий падал с MissingGreenlet: одобрение отдавало
internal_error, кабинет — голый HTTP 500, бот не отвечал на нажатие.

Пользователя могли удалить в панели, а бот (вебхуки в проде выключены) хранит
его id: перед обновлением сервис спрашивает панель, жив ли он, и на 404 идёт
через create_remnawave_user (find-or-create, как купоны). Любой другой ответ —
обновление, как раньше. У проверки свой короткий бюджет, синхронизация после
неё получает весь свой. Истёкшей подписке без сквадов перед созданием
назначаются сквады — иначе пользователь панели был бы без серверов. Строки,
которые стирают «Принудительная очистка» и «Обнулить», — DISABLED: их награда
не оживляет вовсе (ответ владельца 2026-10-09). Сбои панели логируются
warning, ошибки в коде — error с трейсбеком.

Здесь работает настоящий код SubscriptionService на in-memory SQLite, подменён
только клиент Remnawave API. Время панели модельное: «медленный» запрос тратит
модельные секунды бюджетов сервиса, не трогая сроков их настоящих
asyncio.timeout, а у зависшего ближайший бюджет истекает сразу. Тесты не ждут
секундами и не зависят от пауз сборщика мусора: прежний бюджет в 0.2 с
срабатывал посреди работы с базой, когда GC останавливал процесс дольше, и тесты
падали случайно; сдвиг настоящих сроков на модельные секунды оставлял после
9-секундного запроса ~1 с на работу с базой. Запрос в панель вне бюджета
сервиса роняет тест сразу, а не вешает его.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import types
from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.server_squad import get_trial_eligible_server_squads
from app.database.crud.subscription import reset_subscription
from app.database.models import (
    PromoGroup,
    ReferralEarning,
    RewardTask,
    RewardTaskSubmission,
    SentNotification,
    ServerSquad,
    Subscription,
    SubscriptionServer,
    Tariff,
    TrafficPurchase,
    Transaction,
    User,
    UserPromoGroup,
    server_squad_promo_groups,
    tariff_promo_groups,
)
from app.external.remnawave_api import RemnaWaveAPIError, RemnaWaveTransientError, UserStatus as PanelUserStatus
from app.services import grace_access_runtime as grace_module, reward_task_service as service
from app.services.grace_access_runtime import grace_access_runtime
from app.services.grace_access_service import GraceAccessMode
from app.services.remnawave_service import RemnaWaveService
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
    ServerSquad.__table__,
    server_squad_promo_groups,
    SubscriptionServer.__table__,
    Transaction.__table__,
    ReferralEarning.__table__,
]

USER_ID = 1
ADMIN_ID = 2
SUBSCRIPTION_ID = 10
PHOTO_TASK_ID = 100
BOOST_TASK_ID = 101
PANEL_USER_ID = 4242
NEW_PANEL_USER_ID = 5555
FILE_A = 'AgACAgIAAxkBAAIBQ2ZxAAAAAAAAAAAAAAAAAAAA'
SQUADS = ['squad-1']  # сквады подписки по умолчанию: создание обязано их передать
BASE_TARIFF_ID = 20
TRIAL_TARIFF_ID = 30
# Сквады в базе: доступные a, b, c (в этом порядке) и выключенный off.
AVAILABLE_SQUADS = ['squad-a', 'squad-b', 'squad-c']
TRIAL_SQUAD = 'squad-b'  # сквад для выдачи триалов (is_trial_eligible) там, где он нужен тесту

# Страховка: бюджеты сервиса в тестах истекают сразу, но тест не должен висеть,
# даже если код поменяется так, что что-то будет ждать вне них.
GUARD_SECONDS = 30.0


async def _bounded[T](awaitable: Awaitable[T], seconds: float = GUARD_SECONDS) -> T:
    return await asyncio.wait_for(awaitable, seconds)


# Режим → (поведение панели, панельный id пользователя, запросы к API до сбоя).
MODES = {
    # Панель не отвечает дольше бюджета синхронизации: update_remnawave_user отменён.
    'update_timeout': ('hang', PANEL_USER_ID, ['get_user_by_id', 'update_user']),
    # Панельного id нет → create_remnawave_user, и он тоже висит.
    'create_timeout': ('hang', None, ['find_users_by_telegram_id', 'create_user']),
    # Пользователя удалили из панели сразу после проверки: 404 на обновлении → откат →
    # пересоздание читает истёкшую подписку.
    'update_404': ('not_found', PANEL_USER_ID, ['get_user_by_id', 'update_user']),
}

# Какие запросы висят в каком режиме панели. hang — бюджет сервиса истекает сразу;
# stall — бюджеты не трогаем, прервать может только отмена снаружи.
_HANGS = {('hang', 'update_user'), ('hang', 'create_user'), ('lookup_hang', 'get_user_by_id')}
_STALLS = {('lookup_stall', 'get_user_by_id'), ('sync_stall', 'update_user')}


# ── Бюджеты сервиса и модельное время панели ────────────────────────────────


class _UnboundedPanelCall(BaseException):
    """Зависший или медленный запрос в панель вне бюджета сервиса.

    BaseException — чтобы сервис не превратил его в panel_sync_failed: тест должен
    упасть сразу, а не висеть или пройти.
    """


class _Budget:
    """Настоящий asyncio.timeout сервиса, который тест видит; время панели в нём модельное.

    Медленные запросы тратят модельные секунды бюджета (``spent``), а настоящий
    срок не трогают: работа с базой после уложившегося запроса получает весь
    бюджет настоящего времени, и пауза GC или хоста её не обрывает.
    """

    def __init__(self, budgets: _Budgets, delay: float) -> None:
        self._budgets = budgets
        self.delay = delay
        self.spent = 0.0  # модельные секунды запросов в панель внутри бюджета
        self._timeout = asyncio.timeout(delay)

    async def __aenter__(self) -> asyncio.Timeout:
        await self._timeout.__aenter__()
        self._budgets.active.append(self)
        return self._timeout

    async def __aexit__(self, exc_type, exc, tb) -> bool | None:
        self._budgets.active.remove(self)
        try:
            return await self._timeout.__aexit__(exc_type, exc, tb)
        finally:
            if self._timeout.expired():
                self._budgets.expired.append(self.delay)

    def remaining(self) -> float:
        """Остаток в модельном времени."""
        return self.delay - self.spent

    def expire_now(self) -> None:
        """Настоящий таймаут срабатывает сразу — как по истечении срока."""
        self._timeout.reschedule(asyncio.get_running_loop().time())


class _Budgets:
    """Бюджеты (asyncio.timeout) сервиса заданий: открытые сейчас и истёкшие."""

    def __init__(self) -> None:
        self.active: list[_Budget] = []
        self.expired: list[float] = []  # delay истёкших бюджетов по порядку

    def timeout(self, delay: float) -> _Budget:
        return _Budget(self, delay)

    async def spend(self, seconds: float, call: str) -> None:
        """Запрос в панель идёт ``seconds`` секунд — в модельном времени.

        Время копится в каждом открытом бюджете. Если на каком-то траты дойдут
        до его размера, он истекает сразу и отменяет запрос, как настоящий
        таймаут; иначе настоящие сроки не двигаются. ``math.inf`` — панель не
        отвечает вовсе.
        """
        if not self.active:
            raise _UnboundedPanelCall(call)
        first = min(self.active, key=_Budget.remaining)
        if first.remaining() <= seconds:
            first.expire_now()
            await asyncio.Event().wait()  # отменит истёкший бюджет
        for budget in self.active:
            budget.spent += seconds


class FakePanelAPI:
    """Клиент Remnawave, в который ходит настоящий SubscriptionService."""

    def __init__(self, budgets: _Budgets) -> None:
        self.budgets = budgets
        self.mode = 'ok'  # ok | hang | not_found | lookup_hang | lookup_stall | sync_stall
        self.calls: list[str] = []
        self.deleted: set[int] = set()  # id, которых панель больше не знает: 404 на всё
        self.lookup_error: Exception | None = None  # чем ответит get_user_by_id
        self.create_error: Exception | None = None  # чем ответит create_user
        self.delays: dict[str, float] = {}  # модельные секунды запроса, по методу
        self.real_delays: dict[str, float] = {}  # настоящие секунды (asyncio.sleep), по методу
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.stalled = asyncio.Event()  # запрос в режиме stall повис

    @staticmethod
    def _panel_user(panel_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=panel_id,
            telegram_id=555,
            short_uuid='shortuuid',
            subscription_url='https://sub.example/x',
            happ_crypto_link='',
        )

    async def _latency(self, method: str) -> None:
        if (self.mode, method) in _STALLS:
            self.stalled.set()
            await asyncio.Event().wait()
        if (self.mode, method) in _HANGS:
            await self.budgets.spend(math.inf, method)
        if self.delays.get(method):
            await self.budgets.spend(self.delays[method], method)
        if self.real_delays.get(method):
            await asyncio.sleep(self.real_delays[method])

    async def _answer(self, method: str, panel_id: int) -> SimpleNamespace:
        self.calls.append(method)
        await self._latency(method)
        if self.mode == 'not_found' or panel_id in self.deleted:
            raise RemnaWaveAPIError('User not found', 404, {'errorCode': 'A063'})
        return self._panel_user(panel_id)

    async def get_user_by_id(self, user_id):
        # Как настоящий клиент: 404 → None, остальное бросает. В режимах выше
        # пользователь жив — проверка пропускает их к обновлению, как раньше.
        self.calls.append('get_user_by_id')
        await self._latency('get_user_by_id')
        if self.lookup_error is not None:
            raise self.lookup_error
        if user_id in self.deleted:
            return None
        return self._panel_user(user_id)

    async def get_user_by_short_uuid(self, short_uuid):
        """Этот shortUuid панель не знает (404 → None)."""
        self.calls.append('get_user_by_short_uuid')

    async def update_user(self, **kwargs):
        self.updated.append(kwargs)
        return await self._answer('update_user', kwargs['user_id'])

    async def create_user(self, **kwargs):
        self.created.append(kwargs)
        if self.create_error is not None:
            self.calls.append('create_user')
            raise self.create_error
        return await self._answer('create_user', NEW_PANEL_USER_ID)

    async def find_users_by_telegram_id(self, telegram_id):
        self.calls.append('find_users_by_telegram_id')
        return []

    async def find_users_by_email(self, email):
        self.calls.append('find_users_by_email')
        return []


@pytest.fixture(autouse=True)
def _tasks_for_all_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """Раздел открыт всем: здесь проверяется синхронизация, а не режим «только админы»."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', True)


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch) -> FakePanelAPI:
    budgets = _Budgets()
    # Сервис видит обычный asyncio, кроме timeout: его бюджеты отслеживаются.
    tracked_asyncio = types.ModuleType('asyncio')
    tracked_asyncio.__dict__.update(vars(asyncio))
    tracked_asyncio.timeout = budgets.timeout
    monkeypatch.setattr(service, 'asyncio', tracked_asyncio)
    api = FakePanelAPI(budgets)

    @contextlib.asynccontextmanager
    async def _client(self):
        yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', _client)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(settings, 'TRIAL_TARIFF_ID', 0)
    monkeypatch.setattr(grace_access_runtime, '_mode', GraceAccessMode.DISABLED)
    monkeypatch.setattr(RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False))

    from app.services.payment import lava as lava_module

    monkeypatch.setattr(lava_module, 'shift_lava_next_charge_after_manual_extension', AsyncMock())
    notify_bot = MagicMock()
    notify_bot.send_message = AsyncMock()
    notify_bot.session.close = AsyncMock()
    monkeypatch.setattr(service, 'create_bot', lambda: notify_bot)
    api.notify_bot = notify_bot
    service.invalidate_active_tasks_cache()
    return api


_SAME_AS_USER: Any = object()


@contextlib.asynccontextmanager
async def _world(
    monkeypatch: pytest.MonkeyPatch,
    *,
    panel_user_id: int | None,
    subscription_panel_id: int | None = _SAME_AS_USER,
    status: str = 'active',
    end_in_days: int = 5,
    squads: Sequence[str] = SQUADS,
    is_trial: bool = False,
    tariff_id: int | None = None,
    extra: Sequence[Any] = (),
):
    """Пользователь, задания и одна подписка; панельный id подписки по умолчанию — как у пользователя.

    ``extra`` — ещё строки (тарифы, сквады) в той же транзакции.
    """
    if subscription_panel_id is _SAME_AS_USER:
        subscription_panel_id = panel_user_id
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
                    remnawave_id=panel_user_id,
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
                *extra,
                Subscription(
                    id=SUBSCRIPTION_ID,
                    user_id=USER_ID,
                    status=status,
                    is_trial=is_trial,
                    end_date=now + timedelta(days=end_in_days),
                    traffic_limit_gb=0,
                    connected_squads=list(squads),
                    tariff_id=tariff_id,
                    remnawave_short_id='s10',
                    remnawave_id=subscription_panel_id,
                ),
            ]
        )
        await db.commit()
        yield db


async def _load_user(db) -> User:
    """Пользователь так, как его отдаёт get_current_cabinet_user / мидлварь бота."""
    result = await db.execute(
        select(User)
        .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
        .where(User.id == USER_ID)
    )
    return result.scalar_one()


async def _db_state(db) -> tuple:
    """Подписка и отчёты прямо из базы."""
    subscription = (
        await db.execute(select(Subscription.status, Subscription.end_date).where(Subscription.id == SUBSCRIPTION_ID))
    ).one()
    submissions = (
        await db.execute(
            select(RewardTaskSubmission.task_id, RewardTaskSubmission.status, RewardTaskSubmission.source).order_by(
                RewardTaskSubmission.id
            )
        )
    ).all()
    return tuple(subscription), [tuple(row) for row in submissions]


def _boost_bot() -> MagicMock:
    bot = MagicMock()
    boost = SimpleNamespace(expiration_date=datetime.now(UTC) + timedelta(days=30))
    bot.get_user_chat_boosts = AsyncMock(return_value=SimpleNamespace(boosts=[boost]))
    return bot


def _expired_budgets(panel_mode: str) -> list[float]:
    """Какой бюджет истёк: зависшая синхронизация — её собственный, остальное — никакой."""
    return [service.PANEL_SYNC_TIMEOUT_SECONDS] if panel_mode == 'hang' else []


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', list(MODES))
async def test_approve_returns_panel_sync_failed_and_report_stays_pending(monkeypatch, panel, mode):
    panel_mode, panel_user_id, expected_calls = MODES[mode]
    async with _world(monkeypatch, panel_user_id=panel_user_id) as db:
        user = await _load_user(db)
        task = await db.get(RewardTask, PHOTO_TASK_ID)
        submission_id = (await service.submit_photo_report(db, user, task, [{'file_id': FILE_A}])).id
        before = await _db_state(db)
        admin = await db.get(User, ADMIN_ID)
        panel.mode = panel_mode

        results = await _bounded(service.approve(db, [submission_id], admin))

        assert [(r.id, r.ok, r.error_code) for r in results] == [(submission_id, False, 'panel_sync_failed')]
        assert panel.calls == expected_calls  # дошли до настоящих запросов в панель
        assert panel.budgets.expired == _expired_budgets(panel_mode)
        assert all(created['active_internal_squads'] == SQUADS for created in panel.created)
        assert await _db_state(db) == before  # дни не начислены, отчёт на проверке
        assert before[1] == [(PHOTO_TASK_ID, 'pending', 'bot')]
        assert admin.full_name == 'Admin'  # проверяющий перечитан после отката
        panel.notify_bot.send_message.assert_not_awaited()

        # Панель ожила — тот же отчёт одобряется: повтор безопасен.
        panel.mode = 'ok'
        retry = await service.approve(db, [submission_id], admin)
        assert [(r.ok, r.error_code) for r in retry] == [(True, None)]
        status, end_date = (await _db_state(db))[0]
        assert status == 'active'
        assert end_date.replace(tzinfo=UTC) - before[0][1].replace(tzinfo=UTC) == timedelta(days=1)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', list(MODES))
async def test_check_boost_raises_panel_sync_failed_and_caller_objects_stay_usable(monkeypatch, panel, mode):
    panel_mode, panel_user_id, expected_calls = MODES[mode]
    async with _world(monkeypatch, panel_user_id=panel_user_id) as db:
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
        before = await _db_state(db)
        panel.mode = panel_mode

        with pytest.raises(RewardTaskError) as error:
            await _bounded(service.check_boost(db, user, task, _boost_bot()))

        assert error.value.code == 'panel_sync_failed'
        assert panel.calls == expected_calls
        assert panel.budgets.expired == _expired_budgets(panel_mode)
        assert all(created['active_internal_squads'] == SQUADS for created in panel.created)
        # Объекты вызывающего перечитаны после отката: хендлер бота и роут кабинета
        # читают их дальше (язык для текста ошибки), MissingGreenlet быть не должно.
        assert user.language == 'ru'
        assert user.telegram_id == 555
        assert task.title == 'Буст канала'
        assert len(user.subscriptions) == 1
        assert service.format_error_message(error.value, user.language).startswith('Не удалось начислить дни')
        # Ни auto-отчёта, ни продления.
        assert await _db_state(db) == before
        assert before[1] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', list(MODES))
async def test_cabinet_check_boost_answers_503_with_code(monkeypatch, panel, mode):
    from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
    from app.cabinet.routes import reward_tasks as user_routes

    panel_mode, panel_user_id, _ = MODES[mode]
    boost_bot = _boost_bot()

    @contextlib.asynccontextmanager
    async def _create_bot():
        yield boost_bot

    monkeypatch.setattr(user_routes, 'create_bot', _create_bot)

    async with _world(monkeypatch, panel_user_id=panel_user_id) as db:
        app = FastAPI()
        app.include_router(user_routes.router, prefix='/cabinet')
        app.dependency_overrides[get_cabinet_db] = lambda: db

        async def current_user():
            return await _load_user(db)

        app.dependency_overrides[get_current_cabinet_user] = current_user
        panel.mode = panel_mode

        # raise_app_exceptions=False: необработанное исключение — это ответ 500, а не падение теста.
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            response = await _bounded(client.post(f'/cabinet/tasks/{BOOST_TASK_ID}/check-boost'))
            listed = await client.get('/cabinet/tasks')

        assert response.status_code == 503, response.text
        detail = response.json()['detail']
        assert detail['code'] == 'panel_sync_failed'
        assert detail['message'].startswith('Не удалось начислить дни')
        # Ничего не записано — задание по-прежнему доступно.
        statuses = {item['id']: item['status'] for item in listed.json()['tasks']}
        assert statuses[BOOST_TASK_ID] == 'available'


# ── Любой сбой шага синхронизации — panel_sync_failed ───────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'failure',
    [
        'rollback_then_raise',  # как CancelledError-ветка: откат и исключение
        'raise_without_rollback',
        'service_init_fails',  # SubscriptionService() не создался
    ],
)
async def test_any_sync_exception_becomes_panel_sync_failed(monkeypatch, panel, failure):
    async def _sync(self, db, subscription, **kwargs):
        if failure == 'rollback_then_raise':
            await db.rollback()
        raise RuntimeError('panel exploded')

    if failure == 'service_init_fails':

        def _broken_init(self):
            raise RuntimeError('broken configuration')

        monkeypatch.setattr(SubscriptionService, '__init__', _broken_init)
    else:
        monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', _sync)

    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID) as db:
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
        before = await _db_state(db)

        with pytest.raises(RewardTaskError) as error:
            await service.check_boost(db, user, task, _boost_bot())

        assert error.value.code == 'panel_sync_failed'
        assert (user.language, task.title) == ('ru', 'Буст канала')
        assert await _db_state(db) == before


@pytest.mark.asyncio
async def test_failure_after_sync_commit_still_counts_and_restores_objects(monkeypatch, panel):
    """Синхронизация закоммитила, а потом упала (таймаут на закрытии клиента панели).

    Итог берётся из базы: награда есть. Откат после коммита сделал объекты
    вызывающего истёкшими — сервис их перечитывает.
    """

    async def _sync(self, db, subscription, **kwargs):
        await db.commit()
        # Новая транзакция после коммита и её откат: объекты сессии истекают.
        await db.execute(select(1))
        await db.rollback()
        raise TimeoutError

    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', _sync)

    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID) as db:
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
        (_, end_before), _ = await _db_state(db)

        result = await service.check_boost(db, user, task, _boost_bot())

        assert result.reward_days == 3
        assert (user.language, task.title) == ('ru', 'Буст канала')
        (_, end_after), submissions = await _db_state(db)
        assert end_after - end_before == timedelta(days=3)
        assert submissions == [(BOOST_TASK_ID, 'approved', 'auto')]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', list(MODES))
async def test_bot_boost_button_is_answered_with_an_alert(monkeypatch, panel, mode):
    """Бот: нажатие «Проверить буст» получает ответ, а не вечный «часик» на кнопке."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.handlers import reward_tasks as handlers

    panel_mode, panel_user_id, _ = MODES[mode]
    async with _world(monkeypatch, panel_user_id=panel_user_id) as db:
        user = await _load_user(db)
        callback = MagicMock()
        callback.data = f'reward_task_boost:{BOOST_TASK_ID}'
        callback.answer = AsyncMock()
        callback.bot = _boost_bot()
        panel.mode = panel_mode

        # Без error_handler: исключение мимо сервиса уронило бы тест, а не потерялось в логах.
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=555, user_id=555))
        await _bounded(handlers.check_reward_task_boost.__wrapped__(callback, user, db, state))

        assert (await _db_state(db))[1] == []
    callback.answer.assert_awaited_once()
    assert callback.answer.await_args.args[0].startswith('Не удалось начислить дни')
    assert callback.answer.await_args.kwargs == {'show_alert': True}


# ── Пользователя удалили в панели, а бот хранит его id ───────────────────────


def _set_multi_tariff(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: enabled)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _row(db) -> SimpleNamespace:
    """Подписка и привязки к панели прямо из базы."""
    subscription = (
        await db.execute(
            select(
                Subscription.status, Subscription.end_date, Subscription.remnawave_id, Subscription.connected_squads
            ).where(Subscription.id == SUBSCRIPTION_ID)
        )
    ).one()
    user_panel_id = (await db.execute(select(User.remnawave_id).where(User.id == USER_ID))).scalar_one()
    return SimpleNamespace(
        status=subscription.status,
        end=_aware(subscription.end_date),
        remnawave_id=subscription.remnawave_id,
        squads=list(subscription.connected_squads or []),
        user_remnawave_id=user_panel_id,
    )


async def _reward_once(db, entry: str) -> int:
    """Одна попытка: одобрение фотоотчёта или проверка буста. Возвращает дни награды."""
    user = await _load_user(db)
    if entry == 'approve':
        task = await db.get(RewardTask, PHOTO_TASK_ID)
        submission_id = (await service.submit_photo_report(db, user, task, [{'file_id': FILE_A}])).id
        results = await service.approve(db, [submission_id], await db.get(User, ADMIN_ID))
        assert [(r.ok, r.error_code) for r in results] == [(True, None)]
        return 1
    task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
    return (await service.check_boost(db, user, task, _boost_bot())).reward_days


def _assert_extended(before: SimpleNamespace, after: SimpleNamespace, days: int) -> None:
    assert after.status == 'active'
    if before.end > datetime.now(UTC):
        assert after.end - before.end == timedelta(days=days)
    else:
        assert timedelta(days=days, seconds=-30) < after.end - datetime.now(UTC) <= timedelta(days=days)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('tariff_mode', 'status', 'entry'),
    [
        ('single', 'expired', 'approve'),  # прод: маркетолог одобряет фотоотчёт истёкшего
        ('single', 'expired', 'boost'),
        ('single', 'active', 'boost'),
        ('multi', 'expired', 'boost'),
        ('multi', 'active', 'approve'),
    ],
)
async def test_deleted_panel_user_is_recreated_and_rewarded_on_first_attempt(
    monkeypatch, panel, tariff_mode, status, entry
):
    """Раньше обновление получало 404, откатывало начисление и не пересоздавало неактивную
    подписку — panel_sync_failed на каждой попытке. Теперь проверка видит 404, и начисление
    идёт через create_remnawave_user (как купоны): с первой же попытки.
    """
    _set_multi_tariff(monkeypatch, tariff_mode == 'multi')
    end_in_days = -5 if status == 'expired' else 5
    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID, status=status, end_in_days=end_in_days) as db:
        before = await _row(db)
        panel.deleted.add(PANEL_USER_ID)

        days = await _reward_once(db, entry)

        after = await _row(db)
        _assert_extended(before, after, days)
        # Новый пользователь панели сразу включён до нового срока, со сквадами подписки;
        # мёртвый id нигде не остался.
        [created] = panel.created
        assert created['status'] == PanelUserStatus.ACTIVE
        assert _aware(created['expire_at']) == after.end
        assert created['active_internal_squads'] == SQUADS
        assert after.squads == SQUADS
        assert panel.updated == []
        assert after.remnawave_id == NEW_PANEL_USER_ID
        if tariff_mode == 'single':
            assert after.user_remnawave_id == NEW_PANEL_USER_ID
            # Проверка, перепроверка внутри find-or-create, поиск по telegram id, создание.
            assert panel.calls == ['get_user_by_id', 'get_user_by_id', 'find_users_by_telegram_id', 'create_user']
        else:
            assert panel.calls == ['get_user_by_id', 'get_user_by_id', 'create_user']
        submissions = (await _db_state(db))[1]
        assert [(task_id, state) for task_id, state, _ in submissions] == [
            (PHOTO_TASK_ID if entry == 'approve' else BOOST_TASK_ID, 'approved')
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize('subscription_id_alive', [True, False], ids=['subscription_id_alive', 'both_gone'])
async def test_single_tariff_recreates_only_when_every_known_panel_id_is_gone(
    monkeypatch, panel, subscription_id_alive
):
    """Однотарифный режим: id пользователя мёртв, но у подписки свой id (бэкфилл).

    find-or-create стёр бы и его и мог бы завести дубль рядом с живым аккаунтом,
    поэтому создание — только если панель не знает ни один из них. Иначе обновление,
    как раньше (оно упирается в 404 → panel_sync_failed, но дубля нет).
    """
    alive_id = 7777
    async with _world(
        monkeypatch, panel_user_id=PANEL_USER_ID, subscription_panel_id=alive_id, status='expired', end_in_days=-5
    ) as db:
        panel.deleted.add(PANEL_USER_ID)
        if not subscription_id_alive:
            panel.deleted.add(alive_id)
        before = await _db_state(db)
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')

        if subscription_id_alive:
            with pytest.raises(RewardTaskError) as error:
                await service.check_boost(db, user, task, _boost_bot())
            assert error.value.code == 'panel_sync_failed'
            assert panel.created == []
            assert panel.calls == ['get_user_by_id', 'get_user_by_id', 'update_user']
            assert await _db_state(db) == before
        else:
            assert (await service.check_boost(db, user, task, _boost_bot())).reward_days == 3
            [created] = panel.created
            assert created['active_internal_squads'] == SQUADS
            assert (await _row(db)).user_remnawave_id == NEW_PANEL_USER_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'lookup_error',
    [
        RemnaWaveTransientError('Request timed out: GET /api/users/4242'),
        RemnaWaveAPIError('Internal server error', 500),
        RemnaWaveAPIError('Too many requests', 429),
        RuntimeError('unexpected answer'),
    ],
    ids=['transient', 'http_500', 'http_429', 'unexpected'],
)
async def test_unclear_existence_answer_keeps_the_update_path(monkeypatch, panel, lookup_error):
    """Не 404 — не доказательство, что пользователя нет: обновление, как раньше, без создания."""
    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID, status='expired', end_in_days=-5) as db:
        before = await _row(db)
        panel.lookup_error = lookup_error

        days = await _reward_once(db, 'boost')

        _assert_extended(before, await _row(db), days)
        assert panel.calls == ['get_user_by_id', 'update_user']
        assert panel.created == []
        [payload] = panel.updated
        assert (payload['user_id'], payload['status']) == (PANEL_USER_ID, PanelUserStatus.ACTIVE)


@pytest.mark.asyncio
async def test_deleted_panel_user_behind_an_unclear_answer_is_not_recreated(monkeypatch, panel):
    """Пользователя удалили, но на проверку панель ответила таймаутом: создавать нельзя (вдруг жив).

    Обновление получает 404 и честно откатывается — panel_sync_failed, база не тронута.
    Панель снова отвечает — со следующей попытки пересоздание и награда.
    """
    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID, status='expired', end_in_days=-5) as db:
        panel.deleted.add(PANEL_USER_ID)
        panel.lookup_error = RemnaWaveTransientError('Request timed out: GET /api/users/4242')
        before = await _db_state(db)
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')

        with pytest.raises(RewardTaskError) as error:
            await service.check_boost(db, user, task, _boost_bot())

        assert error.value.code == 'panel_sync_failed'
        assert panel.calls == ['get_user_by_id', 'update_user']
        assert panel.created == []
        assert await _db_state(db) == before

        panel.lookup_error = None
        assert (await service.check_boost(db, user, task, _boost_bot())).reward_days == 3
        [created] = panel.created
        assert created['active_internal_squads'] == SQUADS


@pytest.mark.asyncio
async def test_existing_panel_user_keeps_update_path_and_grace_recovery(monkeypatch, panel):
    """Пользователь в панели есть — обновление, как раньше: только оно завершает открытую
    grace-сессию (apply_recovered_grace_update_locked), а оживление истёкших — ровно её случай."""
    recovered: list[tuple[int, dict, str]] = []

    async def _open_grace(db, subscription_ids):
        return set(subscription_ids)

    async def _apply_recovered(db, api, subscription_id, *, update_kwargs, source):
        recovered.append((subscription_id, dict(update_kwargs), source))
        return True, await api.update_user(**update_kwargs)

    monkeypatch.setattr(grace_module, 'lock_grace_sensitive_panel_updates', _open_grace)
    monkeypatch.setattr(grace_module, 'apply_recovered_grace_update_locked', _apply_recovered)

    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID, status='expired', end_in_days=-5) as db:
        before = await _row(db)

        days = await _reward_once(db, 'boost')

        after = await _row(db)
        _assert_extended(before, after, days)
        [(subscription_id, update_kwargs, source)] = recovered
        assert (subscription_id, source) == (SUBSCRIPTION_ID, 'subscription_service.update_remnawave_user')
        assert update_kwargs['user_id'] == PANEL_USER_ID
        assert update_kwargs['status'] == PanelUserStatus.ACTIVE
        assert _aware(update_kwargs['expire_at']) == after.end
        assert panel.calls == ['get_user_by_id', 'update_user']  # update_user — внутри recovery
        assert panel.created == []
        assert (after.remnawave_id, after.user_remnawave_id) == (PANEL_USER_ID, PANEL_USER_ID)


# ── Бюджеты: у проверки свой, синхронизация получает весь свой ──────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('lookup_seconds', 'patch_seconds', 'expired'),
    [
        # Каждый запрос укладывается в свой бюджет, вместе — дольше бюджета синхронизации.
        (2.0, 9.0, []),
        # E2E S8c: панель отвечает на каждый запрос 9 с. Проверка не успела — ответ «неясно»,
        # обновление успело в свои 10 с. В общем бюджете не успевали оба: 6 из 6 отказов.
        (9.0, 9.0, ['lookup']),
    ],
    ids=['each_fits_its_own_budget', 's8c_nine_second_panel'],
)
@pytest.mark.parametrize('entry', ['approve', 'boost'])
async def test_slow_panel_still_grants_when_each_request_fits_its_budget(
    monkeypatch, panel, lookup_seconds, patch_seconds, expired, entry
):
    panel.delays = {'get_user_by_id': lookup_seconds, 'update_user': patch_seconds}
    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID, status='expired', end_in_days=-5) as db:
        before = await _row(db)

        days = await _bounded(_reward_once(db, entry))

        after = await _row(db)
        _assert_extended(before, after, days)
        assert panel.calls == ['get_user_by_id', 'update_user']
        assert panel.created == []
        [payload] = panel.updated
        assert _aware(payload['expire_at']) == after.end
        budget = {'lookup': service.PANEL_LOOKUP_TIMEOUT_SECONDS}
        assert panel.budgets.expired == [budget[name] for name in expired]


def test_panel_budgets_are_pinned():
    """3 с на проверку + 10 с на синхронизацию: до ~13 с держатся блокировки начисления,
    под это посчитаны пачки массового одобрения в кабинете (4 × 13 с < 120 с)."""
    assert service.PANEL_LOOKUP_TIMEOUT_SECONDS == 3.0
    assert service.PANEL_SYNC_TIMEOUT_SECONDS == 10.0


@pytest.mark.asyncio
async def test_lookup_budget_covers_the_whole_lookup_not_each_request(monkeypatch, panel):
    """Однотарифный режим, у пользователя и подписки разные id: оба GET делят один бюджет в 3 с.

    По 2 с на запрос бюджет кончается до ответа по второму id — ответ «неясно», хотя
    панель не знает ни одного из них. Обновление по мёртвому id получает 404:
    panel_sync_failed, база не тронута (компромисс из комментария к
    PANEL_LOOKUP_TIMEOUT_SECONDS). Бюджет на каждый запрос дождался бы обоих 404,
    но держал бы блокировки до 3 + 3 + 10 с.
    """
    subscription_panel_id = 7777
    panel.delays = {'get_user_by_id': 2.0}
    async with _world(
        monkeypatch,
        panel_user_id=PANEL_USER_ID,
        subscription_panel_id=subscription_panel_id,
        status='expired',
        end_in_days=-5,
    ) as db:
        panel.deleted.update({PANEL_USER_ID, subscription_panel_id})
        before = await _db_state(db)
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')

        with pytest.raises(RewardTaskError) as error:
            await _bounded(service.check_boost(db, user, task, _boost_bot()))

        assert error.value.code == 'panel_sync_failed'
        assert panel.budgets.expired == [3.0]
        assert panel.calls == ['get_user_by_id', 'get_user_by_id', 'update_user']
        assert panel.created == []
        assert await _db_state(db) == before


@pytest.mark.asyncio
async def test_lookup_hang_takes_the_update_path_after_its_own_budget(monkeypatch, panel):
    """Проверка висит: её бюджет истекает — ответ «неясно», обновление; бюджет синхронизации цел."""
    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID, status='expired', end_in_days=-5) as db:
        before = await _row(db)
        panel.mode = 'lookup_hang'

        days = await _bounded(_reward_once(db, 'boost'))

        _assert_extended(before, await _row(db), days)
        assert panel.budgets.expired == [service.PANEL_LOOKUP_TIMEOUT_SECONDS]
        assert panel.calls == ['get_user_by_id', 'update_user']
        assert panel.created == []


@pytest.mark.asyncio
async def test_lookup_timeout_never_creates_even_when_the_user_is_gone(monkeypatch, panel):
    """Не успела проверка — не знаем, есть ли пользователь: только обновление, без создания.

    Если его и правда удалили, обновление получает 404 — panel_sync_failed, база не
    тронута (пересоздание из update_remnawave_user падает, известное ограничение).
    """
    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID, status='expired', end_in_days=-5) as db:
        before = await _db_state(db)
        panel.deleted.add(PANEL_USER_ID)
        panel.mode = 'lookup_hang'
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')

        with pytest.raises(RewardTaskError) as error:
            await _bounded(service.check_boost(db, user, task, _boost_bot()))

        assert error.value.code == 'panel_sync_failed'
        assert panel.budgets.expired == [service.PANEL_LOOKUP_TIMEOUT_SECONDS]
        assert panel.calls == ['get_user_by_id', 'update_user']
        assert panel.created == []
        assert await _db_state(db) == before


@pytest.mark.asyncio
async def test_real_lookup_budget_cuts_a_slow_lookup(monkeypatch, panel):
    """Настоящий таймер: бюджет проверки берётся из константы и обрывает долгий запрос.

    Внутри бюджета проверки к базе не ходят, так что короткий бюджет здесь
    безопасен и при паузах GC: где бы он ни сработал, ответ один — «неясно».
    """
    monkeypatch.setattr(service, 'PANEL_LOOKUP_TIMEOUT_SECONDS', 0.05)
    panel.real_delays['get_user_by_id'] = 60.0
    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID, status='expired', end_in_days=-5) as db:
        before = await _row(db)

        days = await _bounded(_reward_once(db, 'boost'), seconds=10.0)

        _assert_extended(before, await _row(db), days)
        assert panel.budgets.expired == [0.05]
        assert panel.calls == ['get_user_by_id', 'update_user']


@pytest.mark.asyncio
@pytest.mark.parametrize('stall', ['lookup_stall', 'sync_stall'])
async def test_cancellation_from_outside_propagates(monkeypatch, panel, stall):
    """Отмену снаружи (остановка бота, обрыв запроса кабинета) бюджеты не превращают в таймаут.

    asyncio.timeout переводит в TimeoutError только собственную отмену — чужая
    доходит до вызывающего, а не становится panel_sync_failed.
    """
    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID, status='expired', end_in_days=-5) as db:
        before = await _db_state(db)
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
        panel.mode = stall

        with structlog.testing.capture_logs() as events:
            grant = asyncio.create_task(service.check_boost(db, user, task, _boost_bot()))
            await _bounded(panel.stalled.wait())
            grant.cancel()
            with pytest.raises(asyncio.CancelledError):
                await _bounded(grant)

        assert grant.cancelled()
        assert panel.budgets.expired == []
        assert [event for event in events if event['event'] in SYNC_FAILURE_EVENTS + LOOKUP_FAILURE_EVENTS] == []
        await db.rollback()
        assert await _db_state(db) == before


# ── Подписка без сквадов, которую синхронизация создаёт в панели ─────────────


def _server_squads(*, trial: Sequence[str] = (), full: Sequence[str] = ()) -> list[ServerSquad]:
    """Доступные a, b, c и выключенный off; ``trial`` выдаются в триал, ``full`` заполнены."""
    rows = [
        ServerSquad(
            id=index,
            squad_uuid=uuid,
            display_name=f'Server {index}',
            sort_order=index,
            is_trial_eligible=uuid in trial,
            max_users=1 if uuid in full else None,
            current_users=1 if uuid in full else 0,
        )
        for index, uuid in enumerate(AVAILABLE_SQUADS, start=1)
    ]
    rows.append(
        ServerSquad(
            id=9,
            squad_uuid='squad-off',
            display_name='Off',
            is_available=False,
            is_trial_eligible='squad-off' in trial,
            current_users=0,
        )
    )
    return rows


def _tariff(tariff_id: int, allowed: Sequence[str], *, trial: bool = False) -> Tariff:
    return Tariff(
        id=tariff_id,
        name=f'Tariff {tariff_id}',
        allowed_squads=list(allowed),
        period_prices={'30': 10000},
        is_trial_available=trial,
    )


async def _counters(db) -> dict[str, int]:
    """Счётчики пользователей серверов."""
    return dict((await db.execute(select(ServerSquad.squad_uuid, ServerSquad.current_users))).all())


def _bumped(squads: Sequence[str]) -> dict[str, int]:
    return {uuid: int(uuid in squads) for uuid in [*AVAILABLE_SQUADS, 'squad-off']}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('row', 'tariffs', 'trial_tariff_setting', 'expected', 'source'),
    [
        # С тарифом — сквады тарифа (так их получают купоны и суточное списание).
        (
            {'tariff_id': BASE_TARIFF_ID},
            [(BASE_TARIFF_ID, ['squad-b', 'squad-c'], False)],
            0,
            ['squad-b', 'squad-c'],
            'tariff',
        ),
        # Пустой список у тарифа — все доступные (get_effective_tariff_squad_uuids).
        ({'tariff_id': BASE_TARIFF_ID}, [(BASE_TARIFF_ID, [], False)], 0, AVAILABLE_SQUADS, 'all_available'),
        # Триал без тарифа — как при активации триала: сквады триального тарифа…
        ({'is_trial': True}, [(TRIAL_TARIFF_ID, ['squad-c'], True)], 0, ['squad-c'], 'trial_tariff'),
        # …или тарифа из TRIAL_TARIFF_ID…
        ({'is_trial': True}, [(TRIAL_TARIFF_ID, ['squad-a'], False)], TRIAL_TARIFF_ID, ['squad-a'], 'trial_tariff'),
        # …а у триального тарифа с пустым списком — все доступные, как у нового триала.
        ({'is_trial': True}, [(TRIAL_TARIFF_ID, [], True)], 0, AVAILABLE_SQUADS, 'all_available'),
        # …а без триального тарифа — один сквад для триалов (подробнее ниже).
        ({'is_trial': True}, [], 0, [TRIAL_SQUAD], 'trial_squad'),
        # Платная без тарифа — все доступные, сквады для триалов ни при чём.
        ({}, [], 0, AVAILABLE_SQUADS, 'all_available'),
    ],
    ids=[
        'tariff',
        'tariff_without_squads',
        'trial_tariff',
        'trial_tariff_setting',
        'trial_tariff_without_squads',
        'trial_no_tariff',
        'no_tariff',
    ],
)
async def test_subscription_without_squads_is_created_with_resolved_squads(
    monkeypatch, panel, row, tariffs, trial_tariff_setting, expected, source
):
    """Нет панельного id — создание; сквады назначаются до него, сохраняются в базе и в счётчиках.

    Сквад для триалов в базе есть всегда: его получает только триал без тарифа и без триального тарифа.
    """
    monkeypatch.setattr(settings, 'TRIAL_TARIFF_ID', trial_tariff_setting)
    extra = [
        *_server_squads(trial=[TRIAL_SQUAD]),
        *(_tariff(tariff_id, allowed, trial=trial) for tariff_id, allowed, trial in tariffs),
    ]
    async with _world(
        monkeypatch, panel_user_id=None, status='expired', end_in_days=-5, squads=[], extra=extra, **row
    ) as db:
        before = await _row(db)

        with structlog.testing.capture_logs() as events:
            days = await _reward_once(db, 'boost')

        after = await _row(db)
        _assert_extended(before, after, days)
        [created] = panel.created
        assert created['active_internal_squads'] == expected
        assert after.squads == expected
        assert await _counters(db) == _bumped(expected)
        [record] = [event for event in events if event['event'].startswith('Подписке без сквадов назначены')]
        assert (record['squads'], record['source']) == (expected, source)
        # Предупреждение «сквадов для триалов нет» — только про триал без сквадов для триалов.
        assert not [event for event in events if event['event'].startswith(NO_TRIAL_SQUADS_EVENT)]


NO_TRIAL_SQUADS_EVENT = 'Сквадов для выдачи триалов нет'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('trial', 'full', 'expected'),
    [
        # Один сквад для триалов — его и получает оживлённый триал, а не все доступные.
        (['squad-b'], [], ['squad-b']),
        # Заполненный не выдаётся, пока есть свободный (get_trial_eligible_server_squads).
        (['squad-a', 'squad-c'], ['squad-a'], ['squad-c']),
        # Из нескольких свободных — один, случайный.
        (['squad-a', 'squad-c'], [], None),
        # Включённых для триалов нет — выключенный, как и у нового триала.
        (['squad-off'], [], ['squad-off']),
    ],
    ids=['one_trial_squad', 'full_one_skipped', 'random_one_of_two', 'only_unavailable'],
)
async def test_trial_without_trial_tariff_gets_one_trial_squad_like_a_fresh_trial(
    monkeypatch, panel, trial, full, expected
):
    """Триал без тарифа и без триального тарифа — как при активации триала (бот, кабинет,
    /start): один случайный сквад для триалов (get_random_trial_squad_uuid), а не все
    доступные. Оживлённый триал не получает больше серверов, чем новый."""
    async with _world(
        monkeypatch,
        panel_user_id=None,
        status='expired',
        end_in_days=-5,
        squads=[],
        is_trial=True,
        extra=_server_squads(trial=trial, full=full),
    ) as db:
        fresh_trial_pool = {squad.squad_uuid for squad in await get_trial_eligible_server_squads(db)}
        counters = await _counters(db)

        with structlog.testing.capture_logs() as events:
            await _reward_once(db, 'boost')

        [created] = panel.created
        [squad] = created['active_internal_squads']
        assert squad in fresh_trial_pool
        assert expected is None or [squad] == expected
        assert (await _row(db)).squads == [squad]
        assert await _counters(db) == {uuid: count + (uuid == squad) for uuid, count in counters.items()}
        [record] = [event for event in events if event['event'].startswith('Подписке без сквадов назначены')]
        assert (record['squads'], record['source']) == ([squad], 'trial_squad')
        assert not [event for event in events if event['event'].startswith(NO_TRIAL_SQUADS_EVENT)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('trial', 'full', 'pool', 'pick', 'expected'),
    [
        # Заполненные в пул не попадают вовсе — при любом выборе из него.
        (['squad-a', 'squad-b', 'squad-c'], ['squad-a', 'squad-c'], ['squad-b'], 0, 'squad-b'),
        (['squad-a', 'squad-b', 'squad-c'], ['squad-a', 'squad-c'], ['squad-b'], -1, 'squad-b'),
        # Из нескольких свободных выбирает random.choice — не «первый попавшийся».
        (['squad-a', 'squad-c'], [], ['squad-a', 'squad-c'], -1, 'squad-c'),
    ],
    ids=['full_skipped_first', 'full_skipped_last', 'random_pick'],
)
async def test_trial_squad_is_picked_at_random_from_the_free_trial_pool(
    monkeypatch, panel, trial, full, pool, pick, expected
):
    """Пул и выбор те же, что у нового триала: свободные сквады для триалов, один случайный."""
    pools: list[list[str]] = []

    def choice(seq):
        pools.append([getattr(item, 'squad_uuid', item) for item in seq])
        return seq[pick]

    monkeypatch.setattr(random, 'choice', choice)
    async with _world(
        monkeypatch,
        panel_user_id=None,
        status='expired',
        end_in_days=-5,
        squads=[],
        is_trial=True,
        extra=_server_squads(trial=trial, full=full),
    ) as db:
        await _reward_once(db, 'boost')

        [created] = panel.created
        assert pools == [pool]
        assert created['active_internal_squads'] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['create', 'update'])
async def test_trial_without_any_trial_squads_gets_all_available_with_a_warning(monkeypatch, panel, path):
    """Сквадов для триалов нет — как create_trial_subscription по умолчанию: все доступные,
    и админ получает предупреждение. Только при создании: обновление сквады не назначает."""
    async with _world(
        monkeypatch,
        panel_user_id=None if path == 'create' else PANEL_USER_ID,
        status='expired',
        end_in_days=-5,
        squads=[],
        is_trial=True,
        extra=_server_squads(),
    ) as db:
        with structlog.testing.capture_logs() as events:
            await _reward_once(db, 'boost')

        warnings = [event for event in events if event['event'].startswith(NO_TRIAL_SQUADS_EVENT)]
        if path == 'create':
            [created] = panel.created
            assert created['active_internal_squads'] == AVAILABLE_SQUADS
            assert (await _row(db)).squads == AVAILABLE_SQUADS
            [warning] = warnings
            assert (warning['log_level'], warning['subscription_id'], warning['squads']) == (
                'warning',
                SUBSCRIPTION_ID,
                AVAILABLE_SQUADS,
            )
        else:
            assert panel.created == []
            assert (await _row(db)).squads == []
            assert warnings == []


@pytest.mark.asyncio
async def test_create_path_keeps_existing_squads(monkeypatch, panel):
    """Сквады у подписки есть — создание передаёт их, ничего не назначается, счётчики не трогаются."""
    async with _world(monkeypatch, panel_user_id=None, squads=['squad-b'], extra=_server_squads()) as db:
        await _reward_once(db, 'boost')

        [created] = panel.created
        assert created['active_internal_squads'] == ['squad-b']
        assert (await _row(db)).squads == ['squad-b']
        assert await _counters(db) == _bumped([])


@pytest.mark.asyncio
async def test_update_path_keeps_panel_squads_of_a_subscription_without_squads(monkeypatch, panel):
    """Обновление живого пользователя панели сквады не трогает: у него они свои (после «Обнулить» — прежние)."""
    async with _world(
        monkeypatch,
        panel_user_id=PANEL_USER_ID,
        status='expired',
        end_in_days=-5,
        squads=[],
        tariff_id=BASE_TARIFF_ID,
        extra=[*_server_squads(), _tariff(BASE_TARIFF_ID, ['squad-b'])],
    ) as db:
        before = await _row(db)

        days = await _reward_once(db, 'boost')

        after = await _row(db)
        _assert_extended(before, after, days)
        [payload] = panel.updated
        assert 'active_internal_squads' not in payload
        assert panel.created == []
        assert after.squads == []
        assert await _counters(db) == _bumped([])


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['api_error', 'hang'])
async def test_failed_create_rolls_back_assigned_squads(monkeypatch, panel, failure):
    """Сквады назначены, но панель не создала пользователя: откат — подписка снова без сквадов.

    Счётчики тоже откатываются, и удачный повтор поднимает их ровно один раз.
    """
    async with _world(
        monkeypatch, panel_user_id=None, status='expired', end_in_days=-5, squads=[], extra=_server_squads()
    ) as db:
        before = await _db_state(db)
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
        if failure == 'api_error':
            panel.create_error = RemnaWaveAPIError('Internal server error', 500)
        else:
            panel.mode = 'hang'

        with pytest.raises(RewardTaskError) as error:
            await _bounded(service.check_boost(db, user, task, _boost_bot()))

        assert error.value.code == 'panel_sync_failed'
        assert panel.created[0]['active_internal_squads'] == AVAILABLE_SQUADS  # до сбоя сквады дошли до запроса
        assert await _db_state(db) == before
        assert (await _row(db)).squads == []
        assert await _counters(db) == _bumped([])

        panel.mode, panel.create_error = 'ok', None
        assert (await service.check_boost(db, user, task, _boost_bot())).reward_days == 3
        assert (await _row(db)).squads == AVAILABLE_SQUADS
        assert await _counters(db) == _bumped(AVAILABLE_SQUADS)


@pytest.mark.asyncio
async def test_without_any_squads_in_db_the_user_is_created_and_admin_is_told(monkeypatch, panel):
    """Назначить нечего (в базе ни одного сквада): как при активации триала — создание без сквадов и error в лог."""
    async with _world(monkeypatch, panel_user_id=None, status='expired', end_in_days=-5, squads=[]) as db:
        with structlog.testing.capture_logs() as events:
            await _reward_once(db, 'boost')

        [created] = panel.created
        assert 'active_internal_squads' not in created
        assert (await _row(db)).squads == []
        [record] = [event for event in events if event['event'].startswith('Подписке без сквадов нечего назначить')]
        assert (record['log_level'], record['subscription_id']) == ('error', SUBSCRIPTION_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize('tariff_id', [None, BASE_TARIFF_ID], ids=['trial_without_tariff', 'tariff'])
async def test_force_cleaned_subscription_is_not_revived(monkeypatch, panel, tariff_id):
    """Настоящая «Принудительная очистка» (force_cleanup_user_data), затем буст.

    Очистка оставляет подписку DISABLED со сроком «сейчас», без сквадов и без
    панельного id пользователя. До 2026-10-09 награда её оживляла и заводила в
    панели заново; теперь подписка «Отключена», раздела у пользователя нет:
    ни запросов в панель, ни сквадов, ни счётчиков.
    """

    @contextlib.asynccontextmanager
    async def _remnawave_client(self):
        yield SimpleNamespace(reset_user_devices=AsyncMock(return_value=True))

    monkeypatch.setattr(RemnaWaveService, 'get_api_client', _remnawave_client)
    extra = [*_server_squads(trial=['squad-c']), _tariff(BASE_TARIFF_ID, ['squad-b'])]
    async with _world(
        monkeypatch,
        panel_user_id=PANEL_USER_ID,
        squads=['squad-old'],  # сквада нет среди серверов: очистке нечего уменьшать
        is_trial=tariff_id is None,
        tariff_id=tariff_id,
        extra=extra,
    ) as db:
        user = (
            await db.execute(select(User).options(selectinload(User.subscriptions)).where(User.id == USER_ID))
        ).scalar_one()
        assert await RemnaWaveService().force_cleanup_user_data(db, user) is True
        panel.deleted.add(PANEL_USER_ID)  # потому и чистили: в панели пользователя нет
        db.expunge_all()
        cleaned = await _row(db)
        assert (cleaned.status, cleaned.squads, cleaned.user_remnawave_id) == ('disabled', [], None)
        counters = await _counters(db)

        user = await _load_user(db)
        access = await service.get_reward_tasks_access(db, user)
        assert (access.visible, access.hidden_reason) == (False, 'subscription_disabled')
        with pytest.raises(RewardTaskError) as error:
            await _reward_once(db, 'boost')
        assert error.value.code == 'tasks_unavailable'

        assert await _row(db) == cleaned
        assert await _counters(db) == counters
        assert panel.calls == []
        assert panel.created == []


@pytest.mark.asyncio
async def test_expired_subscription_without_squads_whose_panel_user_was_deleted_is_recreated_with_squads(
    monkeypatch, panel
):
    """Истёкшая подписка без сквадов (так её оставляет вебхук user.deleted или синк из панели
    без сквадов), а её пользователя потом удалили в панели: проверка видит 404, и награда
    создаёт пользователя заново — со сквадами тарифа, а не без серверов.

    До 2026-10-09 этот сценарий проверялся на «Обнулить» (DISABLED); её награда больше не оживляет.
    """
    async with _world(
        monkeypatch,
        panel_user_id=PANEL_USER_ID,
        status='expired',
        end_in_days=-5,
        squads=[],
        tariff_id=BASE_TARIFF_ID,
        extra=[*_server_squads(), _tariff(BASE_TARIFF_ID, ['squad-b', 'squad-c'])],
    ) as db:
        panel.deleted.add(PANEL_USER_ID)
        before = await _row(db)
        assert (before.status, before.squads) == ('expired', [])

        days = await _reward_once(db, 'boost')

        after = await _row(db)
        _assert_extended(before, after, days)
        [created] = panel.created
        assert created['active_internal_squads'] == ['squad-b', 'squad-c']
        assert after.squads == ['squad-b', 'squad-c']
        assert await _counters(db) == _bumped(['squad-b', 'squad-c'])
        assert panel.calls == ['get_user_by_id', 'get_user_by_id', 'find_users_by_telegram_id', 'create_user']


@pytest.mark.asyncio
async def test_reset_subscription_is_not_revived_even_if_its_panel_user_was_deleted(monkeypatch, panel):
    """«Обнулить» стирает сквады и ставит DISABLED: награда её не оживляет и в панель не ходит."""
    async with _world(
        monkeypatch,
        panel_user_id=PANEL_USER_ID,
        status='expired',
        end_in_days=-5,
        squads=['squad-b'],
        tariff_id=BASE_TARIFF_ID,
        extra=[*_server_squads(), _tariff(BASE_TARIFF_ID, ['squad-b', 'squad-c'])],
    ) as db:
        await reset_subscription(db, await db.get(Subscription, SUBSCRIPTION_ID))
        panel.deleted.add(PANEL_USER_ID)
        before = await _row(db)
        assert (before.status, before.squads) == ('disabled', [])

        with pytest.raises(RewardTaskError) as error:
            await _reward_once(db, 'boost')

        assert error.value.code == 'tasks_unavailable'
        assert await _row(db) == before
        assert await _counters(db) == _bumped([])
        assert panel.calls == []


# ── Уровень логов сбоя синхронизации и проверки ──────────────────────────────

SYNC_FAILURE_EVENTS = (
    'Синхронизация награды за задание с панелью не удалась',
    'Синхронизация награды за задание с панелью упала',
)
LOOKUP_FAILURE_EVENTS = (
    'Не удалось проверить пользователя панели перед начислением награды',
    'Сбой проверки пользователя панели перед начислением награды',
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('failure', 'level', 'error_type'),
    [
        # Ошибка в коде: error с трейсбеком — logging_handler отправит её в админ-чат.
        (RuntimeError('panel exploded'), 'error', 'RuntimeError'),
        # 404 на обновлении → пересоздание читает истёкшую после отката подписку.
        ('update_404', 'error', 'MissingGreenlet'),
        # Сбои панели — штатные: warning без трейсбека.
        (TimeoutError(), 'warning', 'TimeoutError'),
        ('hang', 'warning', 'TimeoutError'),  # настоящий бюджет asyncio.timeout
        (RemnaWaveTransientError('Request timed out'), 'warning', 'RemnaWaveTransientError'),
        (RemnaWaveAPIError('Bad gateway', 502), 'warning', 'RemnaWaveAPIError'),
    ],
    ids=['runtime_error', 'missing_greenlet', 'timeout_error', 'panel_hang', 'transient', 'api_error'],
)
async def test_sync_failure_log_level(monkeypatch, panel, failure, level, error_type):
    if failure == 'hang':
        panel.mode = 'hang'
    elif failure == 'update_404':
        panel.mode = 'not_found'
    else:

        async def _sync(self, db, subscription, **kwargs):
            raise failure

        monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', _sync)

    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID) as db:
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')

        with structlog.testing.capture_logs() as events, pytest.raises(RewardTaskError) as error:
            await _bounded(service.check_boost(db, user, task, _boost_bot()))

    assert error.value.code == 'panel_sync_failed'
    [record] = [event for event in events if event['event'] in SYNC_FAILURE_EVENTS]
    assert (record['log_level'], record['error_type']) == (level, error_type)
    assert record['subscription_id'] == SUBSCRIPTION_ID
    if level == 'error':
        assert record['exc_info'] is True
    else:
        assert 'exc_info' not in record
    # В error (а значит, в админ-чат) уходит только ошибка в коде.
    errors = [event for event in events if event['log_level'] in ('error', 'critical')]
    assert errors == ([record] if level == 'error' else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('lookup', 'level', 'error_type'),
    [
        # Сбои панели — штатные: warning без трейсбека, иначе каждый сбой панели во время
        # массового одобрения уходил бы в админ-чат.
        (RemnaWaveTransientError('Request timed out: GET /api/users/4242'), 'warning', 'RemnaWaveTransientError'),
        (RemnaWaveAPIError('Internal server error', 500), 'warning', 'RemnaWaveAPIError'),
        ('hang', 'warning', 'TimeoutError'),  # собственный бюджет проверки
        # Ошибка в коде: error с трейсбеком, админ должен о ней узнать.
        (RuntimeError('unexpected answer'), 'error', 'RuntimeError'),
    ],
    ids=['transient', 'http_500', 'lookup_timeout', 'unexpected'],
)
async def test_existence_check_failure_log_level(monkeypatch, panel, lookup, level, error_type):
    """Неясный ответ проверки — обновление (награда начислена), а в логе — уровень по причине."""
    if lookup == 'hang':
        panel.mode = 'lookup_hang'
    else:
        panel.lookup_error = lookup

    async with _world(monkeypatch, panel_user_id=PANEL_USER_ID) as db:
        with structlog.testing.capture_logs() as events:
            await _bounded(_reward_once(db, 'boost'))

    assert panel.calls == ['get_user_by_id', 'update_user']
    [record] = [event for event in events if event['event'] in LOOKUP_FAILURE_EVENTS]
    assert (record['log_level'], record['error_type']) == (level, error_type)
    assert record['subscription_id'] == SUBSCRIPTION_ID
    if level == 'error':
        assert record['exc_info'] is True
    else:
        assert 'exc_info' not in record
    errors = [event for event in events if event['log_level'] in ('error', 'critical')]
    assert errors == ([record] if level == 'error' else [])
    assert [event for event in events if event['event'] in SYNC_FAILURE_EVENTS] == []
