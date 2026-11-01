"""Сквозные тесты «Заданий» на настоящей сессии (in-memory SQLite).

Моки тут не годятся: главные риски живут в поведении сессии — синхронизация с
панелью коммитит/откатывает её изнутри, откат делает ORM-объекты истёкшими
(обращение к полю → MissingGreenlet), а от двойной награды защищают частичные
уникальные индексы. Внешнее (панель Remnawave, Lava, Telegram, Redis) заменено
фейками.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    RewardTask,
    RewardTaskSubmission,
    SentNotification,
    ServerSquad,
    Subscription,
    Tariff,
    TrafficPurchase,
    User,
)
from app.services import reward_task_service as service
from app.services.reward_task_service import RewardTaskError
from app.services.subscription_service import SubscriptionService
from app.utils.cache import RateLimitCache
from tests.fixtures.sqlite_memory import memory_session


TABLES = [
    User.__table__,
    Tariff.__table__,
    Subscription.__table__,
    TrafficPurchase.__table__,
    SentNotification.__table__,
    RewardTask.__table__,
    RewardTaskSubmission.__table__,
    # Подписке без сквадов награда ищет сквады заранее (на случай создания в панели).
    ServerSquad.__table__,
]

PHOTO_TASK_ID = 100
BOOST_TASK_ID = 101
USER_ID = 1
ADMIN_ID = 2
SUBSCRIPTION_ID = 10

FILE_A = 'AgACAgIAAxkBAAIBQ2ZxAAAAAAAAAAAAAAAAAAAA'
FILE_B = 'AgACAgIAAxkBAAIBQ2ZxBBBBBBBBBBBBBBBBBBBB'


@pytest.fixture(autouse=True)
def _single_tariff_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    # Раздел открыт всем: здесь проверяется сценарий заданий, а не режим «только админы».
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', True)
    service.invalidate_active_tasks_cache()


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Фейковая синхронизация с панелью, ведущая себя как настоящая по отношению к сессии.

    ok — коммитит и возвращает пользователя панели; fail — откатывает и
    возвращает None; recreated — откатывает (404 → пересоздание) и всё равно
    возвращает пользователя.
    """
    state = SimpleNamespace(mode='ok', calls=[])

    def _fake(kind: str):
        async def _impl(self, db, subscription, **kwargs):
            state.calls.append((kind, subscription.id, subscription.end_date))
            if state.mode == 'fail':
                await db.rollback()
                return None
            if state.mode == 'recreated':
                await db.rollback()
                await db.commit()
                return SimpleNamespace(id=4242)
            await db.commit()
            return SimpleNamespace(id=4242)

        return _impl

    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', _fake('create'))
    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', _fake('update'))

    @contextlib.asynccontextmanager
    async def _panel_client(self):
        # Перед обновлением сервис спрашивает панель, жив ли её пользователь (на 404 — создание).
        yield SimpleNamespace(get_user_by_id=AsyncMock(side_effect=lambda panel_id: SimpleNamespace(id=panel_id)))

    monkeypatch.setattr(SubscriptionService, 'get_api_client', _panel_client)
    return state


@pytest.fixture
def lava(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from app.services.payment import lava as lava_module

    mock = AsyncMock()
    monkeypatch.setattr(lava_module, 'shift_lava_next_charge_after_manual_extension', mock)
    return mock


@pytest.fixture
def notify_bot(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Бот для уведомлений, который сервис создаёт сам (create_bot)."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(service, 'create_bot', lambda: bot)
    return bot


@pytest.fixture
def rate_limit(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=False)
    monkeypatch.setattr(RateLimitCache, 'is_rate_limited', mock)
    return mock


@contextlib.asynccontextmanager
async def _db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subscription_status: str | None = 'active',
    end_in_days: int = 5,
    is_trial: bool = False,
    telegram_id: int | None = 555,
    language: str = 'ru',
    restricted: bool = False,
    remnawave_id: int | None = 4242,
):
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        db.add_all(
            [
                User(
                    id=USER_ID,
                    telegram_id=telegram_id,
                    username='alice',
                    first_name='Alice',
                    language=language,
                    status='active',
                    restriction_subscription=restricted,
                    remnawave_id=remnawave_id,
                ),
                User(id=ADMIN_ID, telegram_id=999, first_name='Admin', language='ru', status='active'),
                RewardTask(
                    id=PHOTO_TASK_ID,
                    kind='photo_report',
                    title='Лайки <канала>',
                    reward_days=2,
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
        if subscription_status is not None:
            db.add(
                Subscription(
                    id=SUBSCRIPTION_ID,
                    user_id=USER_ID,
                    status=subscription_status,
                    is_trial=is_trial,
                    end_date=now + timedelta(days=end_in_days),
                    traffic_limit_gb=0,
                    remnawave_short_id='s10',
                )
            )
        await db.commit()
        yield db


async def _load_user(db, user_id: int = USER_ID) -> User:
    """Пользователь так, как его отдаёт get_current_cabinet_user / мидлварь бота."""
    result = await db.execute(
        select(User)
        .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
        .where(User.id == user_id)
    )
    return result.scalar_one()


async def _subscription_end(db) -> datetime:
    result = await db.execute(select(Subscription.end_date).where(Subscription.id == SUBSCRIPTION_ID))
    end = result.scalar_one()
    return end if end.tzinfo else end.replace(tzinfo=UTC)


async def _submissions(db) -> list[tuple[int, str, str]]:
    result = await db.execute(
        select(RewardTaskSubmission.task_id, RewardTaskSubmission.status, RewardTaskSubmission.source).order_by(
            RewardTaskSubmission.id
        )
    )
    return [tuple(row) for row in result.all()]


async def _submit(db, user: User | None = None, files=(FILE_A,)) -> RewardTaskSubmission:
    user = user or await _load_user(db)
    task = await service.require_active_task(db, PHOTO_TASK_ID, 'photo_report')
    return await service.submit_photo_report(
        db,
        user,
        task,
        [{'file_id': file_id, 'thumb_file_id': file_id} for file_id in files],
        'готово',
        source='cabinet',
    )


async def _admin(db) -> User:
    return await db.get(User, ADMIN_ID)


# ── Фотоотчёт → одобрение ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_extends_subscription_and_notifies(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch) as db:
        before = await _subscription_end(db)
        submission = await _submit(db, files=(FILE_A, FILE_B))
        assert submission.status == 'pending'
        assert [item['file_id'] for item in submission.media_items] == [FILE_A, FILE_B]

        results = await service.approve(db, [submission.id], await _admin(db))

        assert [(r.id, r.ok, r.error_code) for r in results] == [(submission.id, True, None)]
        after = await _subscription_end(db)
        assert after - before == timedelta(days=2)

        stored = (await db.execute(select(RewardTaskSubmission))).scalar_one()
        assert stored.status == 'approved'
        assert stored.reward_days == 2
        assert stored.subscription_id == SUBSCRIPTION_ID
        assert stored.reviewed_by == ADMIN_ID
        assert stored.reviewed_at is not None

    # Панельный id у пользователя есть (single-режим) → update, а не create.
    assert [call[0] for call in panel.calls] == ['update']
    lava.assert_awaited_once()
    assert lava.await_args.args[1:] == (SUBSCRIPTION_ID, 2)
    notify_bot.send_message.assert_awaited_once()
    kwargs = notify_bot.send_message.await_args.kwargs
    assert kwargs['chat_id'] == 555
    # Название экранировано: сообщение уходит в HTML.
    assert kwargs['text'] == '✅ Задание «Лайки &lt;канала&gt;» выполнено: +2 дн. к подписке'
    assert kwargs['parse_mode'] == 'HTML'
    # Кнопка ведёт в раздел заданий бота.
    [[button]] = kwargs['reply_markup'].inline_keyboard
    assert (button.text, button.callback_data) == ('🎁 Задания', 'reward_tasks_menu')
    notify_bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_english_approval_notice_uses_singular_day(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch, language='en') as db:
        task = await db.get(RewardTask, PHOTO_TASK_ID)
        task.reward_days = 1
        await db.commit()
        submission = await _submit(db)

        results = await service.approve(db, [submission.id], await _admin(db))

    assert results[0].ok is True
    kwargs = notify_bot.send_message.await_args.kwargs
    assert kwargs['text'] == '✅ Task «Лайки &lt;канала&gt;» completed: +1 day added to your subscription'
    [[button]] = kwargs['reply_markup'].inline_keyboard
    assert (button.text, button.callback_data) == ('🎁 Tasks', 'reward_tasks_menu')


@pytest.mark.asyncio
async def test_bulk_approve_notifies_only_after_every_grant(monkeypatch, panel, lava, notify_bot):
    """Уведомления уходят после всех начислений: медленный Telegram не держит блокировки."""
    second_task_id = 102
    syncs_at_send: list[int] = []
    notify_bot.send_message.side_effect = lambda **kwargs: syncs_at_send.append(len(panel.calls))

    async with _db(monkeypatch) as db:
        now = datetime.now(UTC)
        db.add(
            RewardTask(
                id=second_task_id,
                kind='photo_report',
                title='Репост',
                reward_days=1,
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()
        user = await _load_user(db)
        first = await _submit(db, user=user)
        second = await service.submit_photo_report(
            db, user, await db.get(RewardTask, second_task_id), [{'file_id': FILE_B}], source='bot'
        )

        results = await service.approve(db, [first.id, second.id], await _admin(db))

    assert [(r.ok, r.error_code) for r in results] == [(True, None), (True, None)]
    assert syncs_at_send == [2, 2]


@pytest.mark.asyncio
async def test_panel_failure_keeps_report_pending_and_days_untouched(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch) as db:
        before = await _subscription_end(db)
        submission_id = (await _submit(db)).id
        panel.mode = 'fail'

        results = await service.approve(db, [submission_id], await _admin(db))

        assert results[0].ok is False
        assert results[0].error_code == 'panel_sync_failed'
        assert await _subscription_end(db) == before
        assert await _submissions(db) == [(PHOTO_TASK_ID, 'pending', 'cabinet')]

        # Отчёт остался в очереди — его можно одобрить, когда панель оживёт.
        # (После отката ORM-объекты сессии истекли — работаем с id.)
        panel.mode = 'ok'
        retry = await service.approve(db, [submission_id], await _admin(db))
        assert retry[0].ok is True
        assert await _subscription_end(db) - before == timedelta(days=2)

    lava.assert_awaited_once()
    notify_bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_that_rolled_back_internally_is_not_reported_as_success(monkeypatch, panel, lava, notify_bot):
    """update_remnawave_user на 404 откатывает сессию и пересоздаёт пользователя панели.

    Он возвращает успех, но начисление уже откачено — итог должен браться из базы.
    """
    async with _db(monkeypatch) as db:
        before = await _subscription_end(db)
        submission_id = (await _submit(db)).id
        panel.mode = 'recreated'

        results = await service.approve(db, [submission_id], await _admin(db))

        assert results[0].error_code == 'panel_sync_failed'
        assert await _subscription_end(db) == before
        assert await _submissions(db) == [(PHOTO_TASK_ID, 'pending', 'cabinet')]
    lava.assert_not_awaited()
    notify_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_approve_of_same_report_is_not_pending(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch) as db:
        before = await _subscription_end(db)
        submission_id = (await _submit(db)).id
        admin = await _admin(db)

        results = await service.approve(db, [submission_id, submission_id, 999_999], admin)
        again = await service.approve(db, [submission_id], admin)

        assert [(r.id, r.ok, r.error_code) for r in results] == [
            (submission_id, True, None),
            (999_999, False, 'not_found'),
        ]
        assert again[0].error_code == 'not_pending'
        assert await _subscription_end(db) - before == timedelta(days=2)


@pytest.mark.asyncio
async def test_pending_report_for_already_completed_task_is_refused(monkeypatch, panel, lava, notify_bot):
    """Грант перепроверяет «уже выполнено» под блокировкой, а не верит статусу отчёта."""
    async with _db(monkeypatch) as db:
        now = datetime.now(UTC)
        db.add_all(
            [
                RewardTaskSubmission(
                    task_id=PHOTO_TASK_ID, user_id=USER_ID, status='approved', source='bot', created_at=now
                ),
                RewardTaskSubmission(
                    id=55, task_id=PHOTO_TASK_ID, user_id=USER_ID, status='pending', source='bot', created_at=now
                ),
            ]
        )
        await db.commit()
        before = await _subscription_end(db)

        results = await service.approve(db, [55], await _admin(db))

        assert results[0].error_code == 'already_completed'
        assert await _subscription_end(db) == before
    assert panel.calls == []


@pytest.mark.asyncio
async def test_expired_subscription_is_revived(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch, subscription_status='expired', end_in_days=-10) as db:
        submission = await _submit(db)

        results = await service.approve(db, [submission.id], await _admin(db))

        assert results[0].ok is True
        subscription = (await db.execute(select(Subscription))).scalar_one()
        assert subscription.status == 'active'
        end = subscription.end_date if subscription.end_date.tzinfo else subscription.end_date.replace(tzinfo=UTC)
        assert timedelta(days=1, hours=23) < end - datetime.now(UTC) <= timedelta(days=2)


@pytest.mark.asyncio
async def test_trial_is_extended_without_becoming_paid(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch, subscription_status='trial', is_trial=True) as db:
        before = await _subscription_end(db)
        submission = await _submit(db)

        results = await service.approve(db, [submission.id], await _admin(db))

        assert results[0].ok is True
        subscription = (await db.execute(select(Subscription))).scalar_one()
        assert subscription.is_trial is True
        assert await _subscription_end(db) - before == timedelta(days=2)


@pytest.mark.asyncio
async def test_user_without_subscription_cannot_submit(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch, subscription_status=None) as db:
        with pytest.raises(RewardTaskError) as error:
            await _submit(db)
        assert error.value.code == 'no_subscription'
        overview = await service.list_tasks_for_user(db, await _load_user(db))
        assert overview.eligible is False
        assert overview.ineligible_reason == 'no_subscription'


@pytest.mark.asyncio
async def test_pending_draft_subscription_does_not_count(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch, subscription_status='pending') as db:
        overview = await service.list_tasks_for_user(db, await _load_user(db))
        assert overview.ineligible_reason == 'no_subscription'


@pytest.mark.asyncio
async def test_restricted_user_is_not_eligible(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch, restricted=True) as db:
        overview = await service.list_tasks_for_user(db, await _load_user(db))
        assert overview.eligible is False
        assert overview.ineligible_reason == 'restricted'
        with pytest.raises(RewardTaskError) as error:
            await _submit(db)
        assert error.value.code == 'restricted'


@pytest.mark.asyncio
async def test_no_panel_id_uses_create(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch, remnawave_id=None) as db:
        submission = await _submit(db)
        results = await service.approve(db, [submission.id], await _admin(db))
    assert results[0].ok is True
    assert [call[0] for call in panel.calls] == ['create']


@pytest.mark.asyncio
async def test_multi_tariff_decides_by_subscription_panel_id(monkeypatch, panel, lava, notify_bot):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)
    async with _db(monkeypatch, remnawave_id=4242) as db:
        # У пользователя id есть, у подписки — нет: в мультитарифе важен id подписки.
        submission = await _submit(db)
        results = await service.approve(db, [submission.id], await _admin(db))
    assert results[0].ok is True
    assert [call[0] for call in panel.calls] == ['create']


@pytest.mark.asyncio
async def test_stale_end_date_in_identity_map_is_not_used(monkeypatch, panel, lava, notify_bot):
    """Подписку продлили мимо этой сессии — награда считается от свежей даты, а не от устаревшей."""
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        stale_end = user.subscriptions[0].end_date
        submission = await _submit(db, user=user)
        fresh_end = datetime.now(UTC) + timedelta(days=40)
        await db.execute(
            update(Subscription)
            .where(Subscription.id == SUBSCRIPTION_ID)
            .values(end_date=fresh_end)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        assert user.subscriptions[0].end_date == stale_end  # identity map не знает о продлении

        results = await service.approve(db, [submission.id], await _admin(db))

        assert results[0].ok is True
        after = await _subscription_end(db)
        assert abs((after - fresh_end) - timedelta(days=2)) < timedelta(seconds=1)


# ── Отклонение и повторная отправка ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_then_resubmit(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch, language='en') as db:
        user = await _load_user(db)
        first = await _submit(db, user=user)

        results = await service.reject(db, [first.id, 424242], await _admin(db), '  не видно <лайков>  ')

        assert [(r.ok, r.error_code) for r in results] == [(True, None), (False, 'not_found')]
        stored = await db.get(RewardTaskSubmission, first.id)
        assert stored.status == 'rejected'
        assert stored.reject_reason == 'не видно <лайков>'
        assert stored.reviewed_by == ADMIN_ID

        view = await service.get_task_view(db, user, PHOTO_TASK_ID)
        assert view.status == 'rejected'
        assert view.reject_reason == 'не видно <лайков>'

        second = await _submit(db, user=user, files=(FILE_B,))
        assert second.status == 'pending'
        view = await service.get_task_view(db, user, PHOTO_TASK_ID)
        assert view.status == 'pending'

        again = await service.reject(db, [first.id], await _admin(db), 'x')
        assert again[0].error_code == 'not_pending'

    notify_bot.send_message.assert_awaited_once()
    kwargs = notify_bot.send_message.await_args.kwargs
    # Причина — отдельной строкой: её финальная точка не склеивается с текстом в «..».
    assert kwargs['text'] == (
        '❌ Your report for the task «Лайки &lt;канала&gt;» was rejected.\n\n'
        'Reason: не видно &lt;лайков&gt;\n\n'
        'You can send a new report.'
    )
    [[button]] = kwargs['reply_markup'].inline_keyboard
    assert (button.text, button.callback_data) == ('📸 Send a new report', f'reward_task:{PHOTO_TASK_ID}')


@pytest.mark.asyncio
async def test_reject_notice_puts_reason_on_its_own_line(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch) as db:
        submission = await _submit(db)

        results = await service.reject(db, [submission.id], await _admin(db), 'На скриншоте не видно лайков.')

    assert results[0].ok is True
    kwargs = notify_bot.send_message.await_args.kwargs
    # Точка в конце причины не склеивается с текстом шаблона в «..».
    assert kwargs['text'] == (
        '❌ Отчёт по заданию «Лайки &lt;канала&gt;» отклонён.\n\n'
        'Причина: На скриншоте не видно лайков.\n\n'
        'Можно отправить новый отчёт.'
    )
    assert '..' not in kwargs['text']
    assert kwargs['parse_mode'] == 'HTML'
    # Кнопка открывает экран задания — там «📸 Отправить новый отчёт».
    [[button]] = kwargs['reply_markup'].inline_keyboard
    assert (button.text, button.callback_data) == ('📸 Отправить новый отчёт', f'reward_task:{PHOTO_TASK_ID}')


@pytest.mark.asyncio
async def test_reject_reason_is_required():
    with pytest.raises(RewardTaskError) as error:
        await service.reject(AsyncMock(), [1], None, '   ')
    assert error.value.code == 'invalid_reason'


# ── Защита от дублей ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_pending_report_is_refused(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        await _submit(db, user=user)
        with pytest.raises(RewardTaskError) as error:
            await _submit(db, user=user, files=(FILE_B,))
        assert error.value.code == 'already_pending'


@pytest.mark.asyncio
async def test_pending_race_is_caught_by_unique_index_without_breaking_session(monkeypatch, panel, lava, notify_bot):
    """Проверка «нет pending» пропустила оба запроса — вставку второго отбивает индекс.

    Откат savepoint не должен задеть объекты вызывающего (пользователь, задание).
    """
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        await _submit(db, user=user)
        task = await db.get(RewardTask, PHOTO_TASK_ID)

        real_has_submission = service._has_submission

        async def blind_pending_check(db_arg, task_id, user_id, status):
            if status == 'pending':
                return False
            return await real_has_submission(db_arg, task_id, user_id, status)

        monkeypatch.setattr(service, '_has_submission', blind_pending_check)
        with pytest.raises(RewardTaskError) as error:
            await service.submit_photo_report(db, user, task, [{'file_id': FILE_B}], None, source='bot')

        assert error.value.code == 'already_pending'
        # Объекты вызывающего живы: обращение к полям не требует ленивой загрузки.
        assert user.language == 'ru'
        assert len(user.subscriptions) == 1
        assert task.title == 'Лайки <канала>'
        assert await _submissions(db) == [(PHOTO_TASK_ID, 'pending', 'cabinet')]


@pytest.mark.asyncio
async def test_partial_unique_indexes(monkeypatch):
    async with _db(monkeypatch) as db:
        now = datetime.now(UTC)

        def row(status: str) -> RewardTaskSubmission:
            return RewardTaskSubmission(
                task_id=PHOTO_TASK_ID, user_id=USER_ID, status=status, source='bot', created_at=now
            )

        # Отклонённых может быть сколько угодно, pending и approved — по одному.
        db.add_all([row('rejected'), row('rejected'), row('pending'), row('approved')])
        await db.commit()

        for status in ('pending', 'approved'):
            db.add(row(status))
            with pytest.raises(IntegrityError):
                await db.flush()
            await db.rollback()


# ── Буст ─────────────────────────────────────────────────────────────────────


def _boost_bot(*expirations: datetime) -> MagicMock:
    bot = MagicMock()
    bot.get_user_chat_boosts = AsyncMock(
        return_value=SimpleNamespace(boosts=[SimpleNamespace(expiration_date=exp) for exp in expirations])
    )
    bot.send_message = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_boost_check_grants_reward_once(monkeypatch, panel, lava, notify_bot, rate_limit):
    async with _db(monkeypatch) as db:
        before = await _subscription_end(db)
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
        bot = _boost_bot(datetime.now(UTC) + timedelta(days=20), datetime.now(UTC) - timedelta(days=1))

        result = await service.check_boost(db, user, task, bot)

        assert result.reward_days == 3
        assert result.subscription_id == SUBSCRIPTION_ID
        assert await _subscription_end(db) - before == timedelta(days=3)
        stored = (await db.execute(select(RewardTaskSubmission))).scalar_one()
        assert (stored.status, stored.source, stored.boost_count, stored.reviewed_by) == ('approved', 'auto', 1, None)
        bot.get_user_chat_boosts.assert_awaited_once_with(chat_id=-1003822976183, user_id=555)
        # Результат показывает сам интерфейс — отдельного уведомления нет.
        bot.send_message.assert_not_awaited()
        notify_bot.send_message.assert_not_awaited()

        overview = await service.list_tasks_for_user(db, user)
        statuses = {view.task.id: view.status for view in overview.tasks}
        assert statuses == {PHOTO_TASK_ID: 'available', BOOST_TASK_ID: 'completed'}

        with pytest.raises(RewardTaskError) as error:
            await service.check_boost(db, user, task, bot)
        assert error.value.code == 'already_completed'
        assert bot.get_user_chat_boosts.await_count == 1


@pytest.mark.asyncio
async def test_boost_panel_failure_leaves_nothing_and_caller_objects_usable(
    monkeypatch, panel, lava, notify_bot, rate_limit
):
    async with _db(monkeypatch) as db:
        before = await _subscription_end(db)
        user = await _load_user(db)
        task = await service.require_active_task(db, BOOST_TASK_ID, 'channel_boost')
        panel.mode = 'fail'

        with pytest.raises(RewardTaskError) as error:
            await service.check_boost(db, user, task, _boost_bot(datetime.now(UTC) + timedelta(days=5)))

        assert error.value.code == 'panel_sync_failed'
        assert await _submissions(db) == []
        assert await _subscription_end(db) == before
        # После отката сервис перечитал объекты вызывающего — хендлер может ими пользоваться.
        assert user.language == 'ru'
        assert user.telegram_id == 555
        assert len(user.subscriptions) == 1
        assert task.title == 'Буст канала'
        assert service.format_error_message(error.value, user.language).startswith('Не удалось начислить дни')


@pytest.mark.asyncio
async def test_not_enough_boosts(monkeypatch, panel, lava, notify_bot, rate_limit):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        task = await db.get(RewardTask, BOOST_TASK_ID)
        task.min_boosts = 2
        await db.commit()

        with pytest.raises(RewardTaskError) as error:
            await service.check_boost(db, user, task, _boost_bot(datetime.now(UTC) + timedelta(days=5)))

        assert error.value.code == 'not_enough_boosts'
        assert error.value.params == {'found': 1, 'required': 2}
        assert await _submissions(db) == []
    assert panel.calls == []


# ── Админка: задания ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_crud_and_counts(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch) as db:
        created = await service.create_task(
            db,
            {'kind': 'channel_boost', 'title': ' Новый ', 'reward_days': 3, 'channel_id': '3822976183'},
        )
        assert created.channel_id == '-1003822976183'
        assert created.title == 'Новый'
        assert created.updated_at is not None

        updated = await service.update_task(db, created, {'title': 'Переименован', 'is_active': False})
        assert updated.title == 'Переименован'
        assert updated.updated_at is not None  # явное значение — поле не истекло после UPDATE

        await _submit(db)
        entries = {entry.task.id: entry for entry in await service.list_tasks_with_counts(db)}
        assert entries[PHOTO_TASK_ID].pending_count == 1
        assert entries[PHOTO_TASK_ID].approved_count == 0
        assert entries[created.id].pending_count == 0

        photo = await db.get(RewardTask, PHOTO_TASK_ID)
        with pytest.raises(RewardTaskError) as error:
            await service.delete_task(db, photo)
        assert error.value.code == 'task_has_submissions'
        with pytest.raises(RewardTaskError) as error:
            await service.update_task(db, photo, {'kind': 'channel_boost', 'channel_id': '-1003822976183'})
        assert error.value.code == 'task_has_submissions'

        await service.delete_task(db, created)
        assert await db.get(RewardTask, created.id) is None
        assert await service.count_active_tasks(db) == 2


@pytest.mark.asyncio
async def test_list_submissions_orders_queue_oldest_first(monkeypatch, panel, lava, notify_bot):
    async with _db(monkeypatch) as db:
        now = datetime.now(UTC)
        db.add_all(
            [
                RewardTaskSubmission(
                    id=1, task_id=PHOTO_TASK_ID, user_id=ADMIN_ID, status='pending', source='bot', created_at=now
                ),
                RewardTaskSubmission(
                    id=2,
                    task_id=PHOTO_TASK_ID,
                    user_id=USER_ID,
                    status='pending',
                    source='bot',
                    created_at=now - timedelta(hours=1),
                ),
                RewardTaskSubmission(
                    id=3,
                    task_id=BOOST_TASK_ID,
                    user_id=USER_ID,
                    status='approved',
                    source='auto',
                    created_at=now - timedelta(days=2),
                    reviewed_at=now,
                ),
                RewardTaskSubmission(
                    id=4,
                    task_id=PHOTO_TASK_ID,
                    user_id=USER_ID,
                    status='rejected',
                    source='bot',
                    created_at=now - timedelta(days=1),
                    reviewed_at=now - timedelta(days=1),
                ),
            ]
        )
        await db.commit()

        pending, total, pending_count = await service.list_submissions(db, status='pending')
        assert [item.id for item in pending] == [2, 1]
        assert (total, pending_count) == (2, 2)
        assert pending[0].user.username == 'alice'
        assert pending[0].task.title == 'Лайки <канала>'

        history, total, pending_count = await service.list_submissions(db, task_id=PHOTO_TASK_ID)
        assert [item.id for item in history] == [1, 2, 4]
        assert (total, pending_count) == (3, 2)

        page, total, _ = await service.list_submissions(db, offset=1, limit=2)
        assert len(page) == 2
        assert total == 4
