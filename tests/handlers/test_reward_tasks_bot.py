"""«Задания» в боте: экраны, проверка буста и сбор фотоотчёта.

Хендлеры гоняются по-настоящему: сервис заданий работает с in-memory SQLite,
FSM — с настоящим хранилищем aiogram, замоканы только Telegram, панель
Remnawave, Lava и Redis. Хендлеры вызываются без ``error_handler``
(``__wrapped__``): иначе любое исключение превратилось бы в «Произошла ошибка»,
и тест прошёл бы мимо поломки.

Главный риск фотоотчёта — альбом: Telegram присылает его пачкой апдейтов,
которые бот обрабатывает параллельно. Тест с «медленным» хранилищем FSM
показывает, что без замка фото теряются, а с замком доходят все и бот отвечает
одним сообщением на пачку.

Кнопки под уведомлениями о проверке нажимают на текстовом сообщении, а не на
экране с логотипом. Эти тесты гоняют настоящие aiogram-сообщения и
``edit_or_answer_photo`` с патчем ``Message.answer``/``edit_text`` (как в проде)
поверх заглушки Bot API — в обоих режимах логотипа.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import itertools
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageMedia,
    EditMessageText,
    SendMessage,
    SendPhoto,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    User as TgUser,
)
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import (
    AdminRole,
    RewardTask,
    RewardTaskSubmission,
    SentNotification,
    ServerSquad,
    Subscription,
    Tariff,
    TrafficPurchase,
    User,
    UserRole,
)
from app.handlers import reward_tasks as handlers
from app.keyboards.reward_tasks import get_reward_tasks_ineligible_keyboard
from app.localization.texts import get_texts
from app.services import reward_task_service as service
from app.services.subscription_service import SubscriptionService
from app.states import PromoCodeStates, RewardTaskStates
from app.utils import message_patch, photo_message
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
    # Режим «только админы»: роли кабинета тоже делают админом.
    AdminRole.__table__,
    UserRole.__table__,
]

USER_ID = 1
TG_ID = 555
SUBSCRIPTION_ID = 10
PHOTO_TASK_ID = 100
BOOST_TASK_ID = 101
SECOND_PHOTO_TASK_ID = 102
INACTIVE_TASK_ID = 103
PROMPT_MESSAGE_ID = 10

_ids = itertools.count(1)


def _file_id(tag: str) -> str:
    """Валидный по формату Telegram file_id (URL-safe base64, 16+ символов)."""
    return f'AgACAgIAAxkBAAI{tag}{next(_ids):06d}'


# ── Фикстуры ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    # Раздел открыт всем; режим «только админы» и скрытый раздел — в своих тестах ниже.
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', True)
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
    monkeypatch.setattr(handlers, 'STATUS_DEBOUNCE_SECONDS', 0)
    service.invalidate_active_tasks_cache()
    handlers._status_tokens.clear()
    handlers._status_tasks.clear()
    yield
    handlers._status_tokens.clear()
    handlers._status_tasks.clear()


@pytest.fixture
def screens(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Экраны раздела (edit_or_answer_photo): что показали и с какими кнопками."""
    mock = AsyncMock()
    monkeypatch.setattr(handlers, 'edit_or_answer_photo', mock)
    return mock


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Синхронизация с панелью, которая, как настоящая, коммитит сессию при успехе."""
    state = SimpleNamespace(calls=[])

    def _fake(kind: str):
        async def _impl(self, db, subscription, **kwargs):
            state.calls.append(kind)
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
def rate_limit(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=False)
    monkeypatch.setattr(RateLimitCache, 'is_rate_limited', mock)
    return mock


@contextlib.asynccontextmanager
async def _db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subscription_status: str | None = 'active',
    subscription_days: float = 5,
    restricted: bool = False,
    had_paid: bool = False,
    photo_task: dict | None = None,
):
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        db.add_all(
            [
                User(
                    id=USER_ID,
                    telegram_id=TG_ID,
                    username='alice',
                    first_name='Alice',
                    language='ru',
                    status='active',
                    restriction_subscription=restricted,
                    has_had_paid_subscription=had_paid,
                    remnawave_id=4242,
                ),
                RewardTask(
                    **{
                        'id': PHOTO_TASK_ID,
                        'kind': 'photo_report',
                        'title': 'Лайки <канала>',
                        'description': 'Поставьте лайки всем постам & пришлите скриншоты',
                        'reward_days': 2,
                        'max_photos': 3,
                        'action_url': 'https://t.me/ledokol_channel',
                        'sort_order': 1,
                        'created_at': now,
                        'updated_at': now,
                        **(photo_task or {}),
                    }
                ),
                RewardTask(
                    id=BOOST_TASK_ID,
                    kind='channel_boost',
                    title='Буст канала',
                    reward_days=3,
                    channel_id='-1003822976183',
                    action_url='https://t.me/boost/ledokol_channel',
                    min_boosts=1,
                    sort_order=2,
                    created_at=now,
                    updated_at=now,
                ),
                RewardTask(
                    id=SECOND_PHOTO_TASK_ID,
                    kind='photo_report',
                    title='Отзыв',
                    reward_days=1,
                    max_photos=1,
                    sort_order=3,
                    created_at=now,
                    updated_at=now,
                ),
                RewardTask(
                    id=INACTIVE_TASK_ID,
                    kind='photo_report',
                    title='Старое задание',
                    reward_days=5,
                    is_active=False,
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
                    is_trial=False,
                    end_date=now + timedelta(days=subscription_days),
                    traffic_limit_gb=0,
                    remnawave_short_id='s10',
                )
            )
        await db.commit()
        yield db


async def _load_user(db) -> User:
    """Пользователь так, как его отдаёт AuthMiddleware (с подписками)."""
    result = await db.execute(
        select(User)
        .options(selectinload(User.subscriptions).selectinload(Subscription.tariff))
        .where(User.id == USER_ID)
    )
    return result.scalar_one()


async def _add_submission(db, task_id: int, status: str, *, reject_reason: str | None = None) -> None:
    now = datetime.now(UTC)
    db.add(
        RewardTaskSubmission(
            task_id=task_id,
            user_id=USER_ID,
            status=status,
            source='bot',
            media_items=[],
            reject_reason=reject_reason,
            created_at=now,
            reviewed_at=None if status == 'pending' else now,
        )
    )
    await db.commit()


async def _submissions(db) -> list[RewardTaskSubmission]:
    result = await db.execute(select(RewardTaskSubmission).order_by(RewardTaskSubmission.id))
    return list(result.scalars().all())


class _SlowStorage(MemoryStorage):
    """Хранилище FSM, которое уступает цикл на каждой операции — как Redis по сети.

    MemoryStorage отвечает мгновенно, и параллельные хендлеры на нём не
    перемешиваются; здесь перемешиваются, как в проде.
    """

    async def get_state(self, key):
        await asyncio.sleep(0)
        return await super().get_state(key)

    async def get_data(self, key):
        await asyncio.sleep(0)
        return await super().get_data(key)

    async def set_data(self, key, data):
        await asyncio.sleep(0)
        await super().set_data(key, data)


def _state(storage: MemoryStorage | None = None) -> FSMContext:
    return FSMContext(storage=storage or MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TG_ID, user_id=TG_ID))


def _bot() -> MagicMock:
    bot = MagicMock()
    message_ids = itertools.count(500)
    bot.send_message = AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(message_id=next(message_ids)))
    bot.delete_message = AsyncMock()
    return bot


def _callback(data: str, bot: MagicMock, *, message_id: int = PROMPT_MESSAGE_ID) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.bot = bot
    callback.from_user = SimpleNamespace(id=TG_ID)
    callback.message = MagicMock()
    callback.message.chat.id = TG_ID
    callback.message.message_id = message_id
    callback.message.edit_text = AsyncMock(return_value=True)
    return callback


def _photo_message(bot: MagicMock, *, caption: str | None = None, media_group_id: str | None = None) -> MagicMock:
    """Фото, как его присылает Telegram: размеры от меньшего к большему."""
    full_id = _file_id('full')
    message = MagicMock()
    message.photo = [
        SimpleNamespace(file_id=_file_id('tiny'), file_unique_id=f'AQADtiny{next(_ids)}', width=90, height=160),
        SimpleNamespace(file_id=_file_id('thumb'), file_unique_id=f'AQADthumb{next(_ids)}', width=320, height=569),
        SimpleNamespace(file_id=full_id, file_unique_id=f'AQADfull{next(_ids)}', width=1280, height=2276),
    ]
    message.caption = caption
    message.media_group_id = media_group_id
    message.chat.id = TG_ID
    message.bot = bot
    message.answer = AsyncMock()
    return message


def _text_message(bot: MagicMock, text: str) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.photo = None
    message.chat.id = TG_ID
    message.bot = bot
    message.answer = AsyncMock()
    return message


def _raw(handler):
    """Хендлер без error_handler — исключения не прячутся."""
    return handler.__wrapped__


def _visible_without_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Раздел виден без запроса в БД — для параллельных апдейтов на одной тестовой сессии."""
    monkeypatch.setattr(
        service, 'get_reward_tasks_access', AsyncMock(return_value=service.RewardTasksAccess(visible=True))
    )


async def _drain_status() -> None:
    """Дождаться отложенных статусов.

    asyncio.wait, а не gather: gather по уже завершённым задачам отдаёт результат,
    не уступая цикл, и done-callback, убирающий задачу из набора, не успевает
    отработать — цикл крутился бы вечно.
    """
    while handlers._status_tasks:
        await asyncio.wait(list(handlers._status_tasks))


def _buttons(markup) -> list[tuple[str, str | None, str | None]]:
    return [(button.text, button.callback_data, button.url) for row in markup.inline_keyboard for button in row]


def _callbacks(markup) -> list[str | None]:
    return [callback_data for _, callback_data, _ in _buttons(markup)]


def _screen(screens: AsyncMock) -> tuple[str, object]:
    kwargs = screens.await_args.kwargs
    return kwargs['caption'], kwargs['keyboard']


async def _enter_report(db, user, state, bot, task_id: int = PHOTO_TASK_ID) -> MagicMock:
    callback = _callback(f'reward_task_photo:{task_id}', bot)
    await _raw(handlers.start_reward_task_report)(callback, user, db, state)
    return callback


# ── Список заданий ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_shows_status_icons_and_rewards(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        await _add_submission(db, BOOST_TASK_ID, 'approved')
        await _add_submission(db, SECOND_PHOTO_TASK_ID, 'pending')
        user = await _load_user(db)
        state = _state()
        await state.set_state(PromoCodeStates.waiting_for_code)  # вход в раздел сбрасывает чужой сценарий
        callback = _callback('reward_tasks_menu', _bot())

        await _raw(handlers.show_reward_tasks)(callback, user, db, state)

    text, keyboard = _screen(screens)
    assert 'Задания' in text
    assert _buttons(keyboard) == [
        ('🎁 Лайки <канала> · +2 дн.', 'reward_task:100', None),
        ('✅ Буст канала · +3 дн.', 'reward_task:101', None),
        ('⏳ Отзыв · +1 дн.', 'reward_task:102', None),
        ('⬅️ Назад', 'back_to_menu', None),
    ]
    assert await state.get_state() is None
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_rejected_task_is_marked_in_list(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        await _add_submission(db, PHOTO_TASK_ID, 'rejected', reject_reason='нет скриншотов')
        user = await _load_user(db)
        await _raw(handlers.show_reward_tasks)(_callback('reward_tasks_menu', _bot()), user, db, _state())

    _, keyboard = _screen(screens)
    assert _buttons(keyboard)[0][0] == '❌ Лайки <канала> · +2 дн.'


@pytest.mark.asyncio
async def test_user_without_subscription_gets_trial_and_buy_buttons(monkeypatch, screens):
    async with _db(monkeypatch, subscription_status=None) as db:
        user = await _load_user(db)
        await _raw(handlers.show_reward_tasks)(_callback('reward_tasks_menu', _bot()), user, db, _state())

    text, keyboard = _screen(screens)
    # Формулировка владельца — из локали сервиса
    assert 'Сначала активируйте пробный период или оформите подписку' in text
    # Что можно будет заработать — без кнопок заданий, названия экранированы
    assert '• Лайки &lt;канала&gt; — +2 дн.' in text
    assert '• Буст канала — +3 дн.' in text
    assert _callbacks(keyboard) == ['menu_trial', 'menu_buy', 'back_to_menu']


@pytest.mark.asyncio
async def test_trial_button_hidden_when_trial_already_used(monkeypatch, screens):
    async with _db(monkeypatch, subscription_status=None, had_paid=True) as db:
        user = await _load_user(db)
        await _raw(handlers.show_reward_tasks)(_callback('reward_tasks_menu', _bot()), user, db, _state())

    _, keyboard = _screen(screens)
    assert _callbacks(keyboard) == ['menu_buy', 'back_to_menu']


@pytest.mark.asyncio
async def test_restricted_user_is_sent_to_support(monkeypatch, screens):
    monkeypatch.setattr(type(settings), 'get_support_contact_url', lambda self: 'https://t.me/ledokol_support')
    async with _db(monkeypatch, restricted=True) as db:
        user = await _load_user(db)
        await _raw(handlers.show_reward_tasks)(_callback('reward_tasks_menu', _bot()), user, db, _state())

    text, keyboard = _screen(screens)
    assert 'ограничено' in text
    assert 'Лайки' not in text  # без превью: выполнить всё равно нельзя
    assert _buttons(keyboard) == [
        ('🆘 Написать в поддержку', None, 'https://t.me/ledokol_support'),
        ('⬅️ Назад', 'back_to_menu', None),
    ]


@pytest.mark.asyncio
async def test_no_active_tasks_shows_empty_screen(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        await db.execute(update(RewardTask).values(is_active=False))
        await db.commit()
        user = await _load_user(db)
        await _raw(handlers.show_reward_tasks)(_callback('reward_tasks_menu', _bot()), user, db, _state())

    text, keyboard = _screen(screens)
    assert 'Сейчас нет доступных заданий' in text
    assert _callbacks(keyboard) == ['back_to_menu']


@pytest.mark.asyncio
async def test_tasks_command_sends_list_as_new_message(monkeypatch):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        message = _text_message(_bot(), '/tasks')

        await _raw(handlers.cmd_tasks)(message, user, db, _state())

    text = message.answer.await_args.args[0]
    keyboard = message.answer.await_args.kwargs['reply_markup']
    assert 'Выполняйте задания' in text
    assert 'reward_task:100' in _callbacks(keyboard)


# ── Экран задания ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_photo_task_detail(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        await _raw(handlers.show_reward_task)(_callback('reward_task:100', _bot()), user, db, _state())

    text, keyboard = _screen(screens)
    assert '<b>Лайки &lt;канала&gt;</b>' in text
    assert 'Поставьте лайки всем постам &amp; пришлите скриншоты' in text
    assert 'Награда: <b>+2 дн.</b>' in text
    assert 'пришлите скриншоты в подтверждение (до 3 шт.)' in text
    assert _buttons(keyboard) == [
        ('🔗 Открыть ссылку', None, 'https://t.me/ledokol_channel'),
        ('📸 Отправить отчёт', 'reward_task_photo:100', None),
        ('⬅️ К заданиям', 'reward_tasks_menu', None),
    ]


@pytest.mark.asyncio
async def test_boost_task_detail(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        await _raw(handlers.show_reward_task)(_callback('reward_task:101', _bot()), user, db, _state())

    text, keyboard = _screen(screens)
    assert 'Нужно бустов: 1' in text
    assert _buttons(keyboard) == [
        ('🚀 Забустить канал', None, 'https://t.me/boost/ledokol_channel'),
        ('🔄 Проверить буст', 'reward_task_boost:101', None),
        ('⬅️ К заданиям', 'reward_tasks_menu', None),
    ]


@pytest.mark.asyncio
async def test_rejected_task_shows_reason_and_offers_new_report(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        await _add_submission(db, PHOTO_TASK_ID, 'rejected', reject_reason='не видно <лайков>')
        user = await _load_user(db)
        await _raw(handlers.show_reward_task)(_callback('reward_task:100', _bot()), user, db, _state())

    text, keyboard = _screen(screens)
    assert 'Отчёт отклонён: не видно &lt;лайков&gt;' in text
    assert ('📸 Отправить новый отчёт', 'reward_task_photo:100', None) in _buttons(keyboard)


@pytest.mark.asyncio
async def test_completed_and_pending_tasks_have_no_action(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        await _add_submission(db, BOOST_TASK_ID, 'approved')
        await _add_submission(db, PHOTO_TASK_ID, 'pending')
        user = await _load_user(db)

        await _raw(handlers.show_reward_task)(_callback('reward_task:101', _bot()), user, db, _state())
        completed_text, completed_keyboard = _screen(screens)
        await _raw(handlers.show_reward_task)(_callback('reward_task:100', _bot()), user, db, _state())
        pending_text, pending_keyboard = _screen(screens)

    assert 'Задание выполнено' in completed_text
    assert _callbacks(completed_keyboard) == ['reward_tasks_menu']  # и без ссылки на буст
    assert 'Отчёт на проверке' in pending_text
    assert _callbacks(pending_keyboard) == [None, 'reward_tasks_menu']  # ссылка задания осталась


@pytest.mark.asyncio
async def test_inactive_task_falls_back_to_list(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        callback = _callback('reward_task:103', _bot())
        await _raw(handlers.show_reward_task)(callback, user, db, _state())

    assert callback.answer.await_args.args[0] == 'Это задание больше недоступно.'
    assert callback.answer.await_args.kwargs['show_alert'] is True
    _, keyboard = _screen(screens)
    assert 'reward_task:100' in _callbacks(keyboard)
    assert 'reward_task:103' not in _callbacks(keyboard)


@pytest.mark.asyncio
async def test_broken_action_url_drops_button_but_keeps_screen(monkeypatch, screens):
    """Кривая URL-кнопка уронила бы всё сообщение (Telegram отвергает его целиком)."""
    async with _db(monkeypatch, photo_task={'action_url': 'javascript:alert(1)'}) as db:
        user = await _load_user(db)
        await _raw(handlers.show_reward_task)(_callback('reward_task:100', _bot()), user, db, _state())

    _, keyboard = _screen(screens)
    assert all(url is None for _, _, url in _buttons(keyboard))


# ── Буст ─────────────────────────────────────────────────────────────────────


def _boost_bot(*expirations: datetime) -> MagicMock:
    bot = _bot()
    bot.get_user_chat_boosts = AsyncMock(
        return_value=SimpleNamespace(boosts=[SimpleNamespace(expiration_date=exp) for exp in expirations])
    )
    return bot


@pytest.mark.asyncio
async def test_boost_check_grants_days_and_shows_success(monkeypatch, screens, panel, lava, rate_limit):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        bot = _boost_bot(datetime.now(UTC) + timedelta(days=20))
        callback = _callback('reward_task_boost:101', bot)

        await _raw(handlers.check_reward_task_boost)(callback, user, db, _state())

        stored = await _submissions(db)
        end_date = (await db.execute(select(Subscription.end_date))).scalar_one()

    assert [(s.task_id, s.status, s.source, s.boost_count) for s in stored] == [(BOOST_TASK_ID, 'approved', 'auto', 1)]
    bot.get_user_chat_boosts.assert_awaited_once_with(chat_id=-1003822976183, user_id=TG_ID)
    end_date = end_date if end_date.tzinfo else end_date.replace(tzinfo=UTC)
    assert end_date - datetime.now(UTC) > timedelta(days=7)
    text, keyboard = _screen(screens)
    assert 'Буст засчитан' in text
    assert '+3 дн.' in text
    assert 'Подписка действует до' in text
    assert _callbacks(keyboard) == ['reward_tasks_menu', 'back_to_menu']
    callback.answer.assert_awaited_once_with()
    assert panel.calls == ['update']


@pytest.mark.asyncio
async def test_boost_not_found_is_an_alert_on_the_same_screen(monkeypatch, screens, panel, lava, rate_limit):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        callback = _callback('reward_task_boost:101', _boost_bot(datetime.now(UTC) - timedelta(days=1)))

        await _raw(handlers.check_reward_task_boost)(callback, user, db, _state())

        assert await _submissions(db) == []
    callback.answer.assert_awaited_once_with(
        'Бустов найдено: 0 из 1. Забустите канал и проверьте ещё раз.', show_alert=True
    )
    screens.assert_not_awaited()


@pytest.mark.asyncio
async def test_boost_without_subscription_shows_ineligible_screen(monkeypatch, screens, panel, lava, rate_limit):
    async with _db(monkeypatch, subscription_status=None) as db:
        user = await _load_user(db)
        bot = _boost_bot(datetime.now(UTC) + timedelta(days=20))
        callback = _callback('reward_task_boost:101', bot)

        await _raw(handlers.check_reward_task_boost)(callback, user, db, _state())

    bot.get_user_chat_boosts.assert_not_awaited()
    text, keyboard = _screen(screens)
    assert 'Сначала активируйте пробный период' in text
    assert 'menu_buy' in _callbacks(keyboard)
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_boost_on_completed_task_refreshes_detail(monkeypatch, screens, panel, lava, rate_limit):
    async with _db(monkeypatch) as db:
        await _add_submission(db, BOOST_TASK_ID, 'approved')
        user = await _load_user(db)
        callback = _callback('reward_task_boost:101', _boost_bot(datetime.now(UTC) + timedelta(days=20)))

        await _raw(handlers.check_reward_task_boost)(callback, user, db, _state())

    assert callback.answer.await_args.args[0] == 'Вы уже выполнили это задание — награда начислена.'
    text, keyboard = _screen(screens)
    assert 'Задание выполнено' in text
    assert _callbacks(keyboard) == ['reward_tasks_menu']


# ── Фотоотчёт ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_entry_turns_task_screen_into_prompt(monkeypatch):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        callback = await _enter_report(db, user, state, _bot())

    assert await state.get_state() == RewardTaskStates.waiting_for_photos.state
    data = await state.get_data()
    assert data['reward_task_id'] == PHOTO_TASK_ID
    assert data['reward_task_max_photos'] == 3
    assert data['reward_task_photos'] == []
    assert data['reward_task_status_message_id'] == PROMPT_MESSAGE_ID
    assert data['reward_task_title'] == 'Лайки <канала>'
    assert data['reward_task_excerpt'] == 'Поставьте лайки всем постам & пришлите скриншоты'
    text = callback.message.edit_text.await_args.args[0]
    # Что снимать — описание задания — остаётся перед глазами
    assert text == (
        '📸 <b>Отчёт: Лайки &lt;канала&gt;</b>\n\n'
        'Поставьте лайки всем постам &amp; пришлите скриншоты\n\n'
        'Пришлите скриншоты (до 3 шт.) — по одному или альбомом, затем нажмите «Готово».\n'
        'Подпись к фото увидит администратор, который проверяет отчёт.'
    )
    # «Готово» появится, когда будет что отправить
    assert _callbacks(callback.message.edit_text.await_args.kwargs['reply_markup']) == ['reward_task_cancel:100']


@pytest.mark.asyncio
async def test_album_is_collected_completely_with_one_status(monkeypatch):
    monkeypatch.setattr(handlers, 'STATUS_DEBOUNCE_SECONDS', 0.05)
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state(_SlowStorage())
        bot = _bot()
        await _enter_report(db, user, state, bot)
        album = [
            _photo_message(bot, caption='мой ник @alice', media_group_id='album-1'),
            _photo_message(bot, media_group_id='album-1'),
            _photo_message(bot, media_group_id='album-1'),
        ]
        # В проде у каждого апдейта своя сессия БД; здесь одна на всех, а параллельно
        # пользоваться одной AsyncSession нельзя — видимость раздела подменяем.
        _visible_without_db(monkeypatch)

        await asyncio.gather(*(_raw(handlers.collect_reward_task_photo)(m, user, state, db) for m in album))
        await _drain_status()

    data = await state.get_data()
    assert {item['file_id'] for item in data['reward_task_photos']} == {m.photo[-1].file_id for m in album}
    first = next(item for item in data['reward_task_photos'] if item['file_id'] == album[0].photo[-1].file_id)
    assert first['thumb_file_id'] == album[0].photo[1].file_id  # превью ~320px
    assert data['reward_task_comment'] == 'мой ник @alice'
    # Один ответ на всю пачку, подсказка-приглашение убрана
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs['chat_id'] == TG_ID
    assert 'Получено скриншотов: 3 из 3' in kwargs['text']
    assert _callbacks(kwargs['reply_markup']) == ['reward_task_done:100', 'reward_task_cancel:100']
    bot.delete_message.assert_awaited_once_with(chat_id=TG_ID, message_id=PROMPT_MESSAGE_ID)
    assert data['reward_task_status_message_id'] == 500


@pytest.mark.asyncio
async def test_album_without_user_lock_loses_photos(monkeypatch):
    """Контроль для теста выше: «медленное» хранилище действительно даёт гонку."""

    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state(_SlowStorage())
        bot = _bot()
        await _enter_report(db, user, state, bot)
        monkeypatch.setattr(handlers, '_user_lock', lambda user_id: asyncio.Lock())  # свой замок на каждый апдейт
        album = [_photo_message(bot, media_group_id='album-1') for _ in range(3)]
        _visible_without_db(monkeypatch)  # одна сессия на параллельные апдейты (см. тест выше)

        await asyncio.gather(*(_raw(handlers.collect_reward_task_photo)(m, user, state, db) for m in album))
        await _drain_status()

    assert len((await state.get_data())['reward_task_photos']) < 3


@pytest.mark.asyncio
async def test_photos_over_limit_are_dropped_with_notice(monkeypatch):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot, task_id=SECOND_PHOTO_TASK_ID)  # max_photos=1
        for message in (_photo_message(bot), _photo_message(bot)):
            await _raw(handlers.collect_reward_task_photo)(message, user, state, db)
        await _drain_status()

    assert len((await state.get_data())['reward_task_photos']) == 1
    text = bot.send_message.await_args.kwargs['text']
    assert 'Получено скриншотов: 1 из 1' in text
    assert 'Это максимум' in text
    assert 'Скриншотов — не больше 1, лишние не добавлены.' in text


@pytest.mark.asyncio
async def test_same_photo_twice_counts_once(monkeypatch):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot)
        message = _photo_message(bot)
        await _raw(handlers.collect_reward_task_photo)(message, user, state, db)
        await _raw(handlers.collect_reward_task_photo)(message, user, state, db)
        await _drain_status()

    assert len((await state.get_data())['reward_task_photos']) == 1


@pytest.mark.asyncio
async def test_non_photo_input_gets_photo_only_hint(monkeypatch):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot)

        await _raw(handlers.remind_photo_only)(_text_message(bot, 'вот мой отчёт'), user, state, db)
        await _drain_status()

    assert await state.get_state() == RewardTaskStates.waiting_for_photos.state
    kwargs = bot.send_message.await_args.kwargs
    assert 'Получено скриншотов: 0 из 3' in kwargs['text']
    assert 'Принимаются только фото' in kwargs['text']
    assert _callbacks(kwargs['reply_markup']) == ['reward_task_cancel:100']
    assert (await state.get_data())['reward_task_not_photo'] is False  # подсказка показана — флаг сброшен


@pytest.mark.asyncio
async def test_done_creates_pending_report_from_bot(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot)
        photos = [_photo_message(bot, caption='ник @alice'), _photo_message(bot)]
        for message in photos:
            await _raw(handlers.collect_reward_task_photo)(message, user, state, db)
        await _drain_status()
        status_message_id = (await state.get_data())['reward_task_status_message_id']
        # Сколько статусов уже было, зависит от пауз между фото (каждое фото сперва
        # спрашивает видимость раздела у БД), — считаем от этого места.
        sends_before = bot.send_message.await_count
        # Ещё одно фото — и сразу «Готово», раньше отложенного статуса
        monkeypatch.setattr(handlers, 'STATUS_DEBOUNCE_SECONDS', 0.05)
        last = _photo_message(bot)
        await _raw(handlers.collect_reward_task_photo)(last, user, state, db)
        callback = _callback('reward_task_done:100', bot, message_id=status_message_id - 1)

        await _raw(handlers.submit_reward_task_report)(callback, user, db, state)
        await _drain_status()

        stored = await _submissions(db)

    assert len(stored) == 1
    submission = stored[0]
    assert (submission.task_id, submission.status, submission.source) == (PHOTO_TASK_ID, 'pending', 'bot')
    assert [item['file_id'] for item in submission.media_items] == [m.photo[-1].file_id for m in [*photos, last]]
    assert all(item['thumb_file_id'] for item in submission.media_items)
    assert submission.comment == 'ник @alice'
    assert await state.get_state() is None
    text, keyboard = _screen(screens)
    assert 'Отчёт отправлен на проверку' in text
    assert _callbacks(keyboard) == ['reward_tasks_menu', 'back_to_menu']
    # Статус с кнопками удалён, отложенный статус после «Готово» не пришёл
    bot.delete_message.assert_any_await(chat_id=TG_ID, message_id=status_message_id)
    assert bot.send_message.await_count == sends_before


@pytest.mark.asyncio
async def test_done_without_photos_is_refused(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot)
        callback = _callback('reward_task_done:100', bot)

        await _raw(handlers.submit_reward_task_report)(callback, user, db, state)

        assert await _submissions(db) == []
    callback.answer.assert_awaited_once_with('Пришлите хотя бы один скриншот.', show_alert=True)
    assert await state.get_state() == RewardTaskStates.waiting_for_photos.state


@pytest.mark.parametrize('pressed', ['reward_task_done:100', 'reward_task_done:102'])
@pytest.mark.asyncio
async def test_stale_done_button_sends_nothing(monkeypatch, screens, pressed):
    """«Готово» со старого сообщения: сбор уже закончен или идёт по другому заданию."""
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        if pressed.endswith(':102'):
            await _enter_report(db, user, state, bot)
            await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)
        callback = _callback(pressed, bot)

        await _raw(handlers.submit_reward_task_report)(callback, user, db, state)
        await _drain_status()

        assert await _submissions(db) == []
    callback.answer.assert_awaited_once_with('Этот отчёт уже отправлен или отменён.', show_alert=True)
    text, _ = _screen(screens)
    assert '<b>' in text  # экран задания с актуальным статусом


@pytest.mark.asyncio
async def test_double_tap_on_done_creates_one_report(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state(_SlowStorage())
        bot = _bot()
        await _enter_report(db, user, state, bot)
        await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)
        first, second = _callback('reward_task_done:100', bot), _callback('reward_task_done:100', bot)
        _visible_without_db(monkeypatch)  # одна сессия на параллельные нажатия (в проде у каждого своя)

        await asyncio.gather(
            _raw(handlers.submit_reward_task_report)(first, user, db, state),
            _raw(handlers.submit_reward_task_report)(second, user, db, state),
        )
        await _drain_status()

        assert len(await _submissions(db)) == 1
    alerts = [c.answer.await_args.args[0] if c.answer.await_args.args else None for c in (first, second)]
    assert sorted(alerts, key=str) == sorted([None, 'Этот отчёт уже отправлен или отменён.'], key=str)


@pytest.mark.asyncio
async def test_cancel_leaves_flow_and_returns_to_task(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot)
        await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)
        await _drain_status()
        monkeypatch.setattr(handlers, 'STATUS_DEBOUNCE_SECONDS', 0.05)
        await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)  # статус ещё в пути
        callback = _callback('reward_task_cancel:100', bot, message_id=999)

        await _raw(handlers.cancel_reward_task_report)(callback, user, db, state)
        await _drain_status()

        assert await _submissions(db) == []
    assert await state.get_state() is None
    callback.answer.assert_awaited_once_with('Отправка отчёта отменена.')
    text, keyboard = _screen(screens)
    assert 'Лайки' in text
    assert 'reward_task_photo:100' in _callbacks(keyboard)
    assert bot.send_message.await_count == 1  # отложенный статус отменён
    bot.delete_message.assert_any_await(chat_id=TG_ID, message_id=500)


@pytest.mark.asyncio
async def test_reentry_keeps_photos_for_same_task_and_resets_for_another(monkeypatch):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot)
        await _raw(handlers.collect_reward_task_photo)(_photo_message(bot, caption='ник'), user, state, db)
        await _drain_status()

        again = await _enter_report(db, user, state, bot)  # кнопка со старого экрана задания
        same = await state.get_data()
        other = await _enter_report(db, user, state, bot, task_id=SECOND_PHOTO_TASK_ID)
        switched = await state.get_data()

    assert len(same['reward_task_photos']) == 1
    assert same['reward_task_comment'] == 'ник'
    assert 'Получено скриншотов: 1 из 3' in again.message.edit_text.await_args.args[0]
    assert _callbacks(again.message.edit_text.await_args.kwargs['reply_markup']) == [
        'reward_task_done:100',
        'reward_task_cancel:100',
    ]
    bot.delete_message.assert_any_await(chat_id=TG_ID, message_id=500)  # прежний статус убран
    assert other.message.edit_text.await_args is not None
    assert switched['reward_task_id'] == SECOND_PHOTO_TASK_ID
    assert switched['reward_task_photos'] == []
    assert switched['reward_task_comment'] is None


@pytest.mark.asyncio
async def test_entry_refused_while_report_is_pending(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        await _add_submission(db, PHOTO_TASK_ID, 'pending')
        user = await _load_user(db)
        state = _state()
        callback = await _enter_report(db, user, state, _bot())

    callback.answer.assert_awaited_once_with('Ваш отчёт по этому заданию уже на проверке.', show_alert=True)
    assert await state.get_state() is None
    text, _ = _screen(screens)
    assert 'Отчёт на проверке' in text


@pytest.mark.asyncio
async def test_entry_without_subscription_shows_ineligible_screen(monkeypatch, screens):
    async with _db(monkeypatch, subscription_status=None) as db:
        user = await _load_user(db)
        state = _state()
        await _enter_report(db, user, state, _bot())

    assert await state.get_state() is None
    text, _ = _screen(screens)
    assert 'Сначала активируйте пробный период' in text


@pytest.mark.asyncio
async def test_done_after_task_was_switched_off(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot)
        await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)
        await _drain_status()
        await db.execute(update(RewardTask).where(RewardTask.id == PHOTO_TASK_ID).values(is_active=False))
        await db.commit()
        callback = _callback('reward_task_done:100', bot)

        await _raw(handlers.submit_reward_task_report)(callback, user, db, state)

        assert await _submissions(db) == []
    callback.answer.assert_awaited_once_with('Это задание больше недоступно.', show_alert=True)
    assert await state.get_state() is None
    _, keyboard = _screen(screens)
    assert 'reward_task:100' not in _callbacks(keyboard)


@pytest.mark.asyncio
async def test_done_after_limit_was_lowered_restarts_collection(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot)
        for _ in range(3):
            await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)
        await _drain_status()
        await db.execute(update(RewardTask).where(RewardTask.id == PHOTO_TASK_ID).values(max_photos=2))
        await db.commit()
        callback = _callback('reward_task_done:100', bot)

        await _raw(handlers.submit_reward_task_report)(callback, user, db, state)
        await _drain_status()

        assert await _submissions(db) == []
    callback.answer.assert_awaited_once_with(
        'Слишком много скриншотов — можно не больше 2. Пришлите скриншоты заново.', show_alert=True
    )
    data = await state.get_data()
    assert await state.get_state() == RewardTaskStates.waiting_for_photos.state
    assert data['reward_task_photos'] == []
    assert data['reward_task_max_photos'] == 2
    assert 'Получено скриншотов: 0 из 2' in bot.send_message.await_args.kwargs['text']


@pytest.mark.asyncio
async def test_leaving_to_tasks_list_drops_unsent_report(monkeypatch, screens):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot)
        await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)
        await _drain_status()

        await _raw(handlers.cmd_tasks)(_text_message(bot, '/tasks'), user, db, state)

    assert await state.get_state() is None
    assert await state.get_data() == {}
    bot.delete_message.assert_any_await(chat_id=TG_ID, message_id=500)


@pytest.mark.asyncio
async def test_photo_after_flow_ended_is_ignored(monkeypatch):
    """Фото дождалось замка уже после «Готово»/«Отмены» — сценарий не воскресает."""
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()

        await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)
        await _drain_status()

    assert await state.get_state() is None
    assert await state.get_data() == {}
    bot.send_message.assert_not_awaited()


# ── Подсказка и статус: что снимать ──────────────────────────────────────────

LONG_DESCRIPTION = (
    '1. Подпишитесь на канал <LEDOKOL>.\n'
    '2. Поставьте реакцию на 5 последних постов & сделайте скриншот каждого.\n'
    '3. ' + 'Пришлите скриншоты одним альбомом. ' * 40
).strip()


def _visible(text: str) -> str:
    """Текст так, как его считает Telegram: без тегов, с раскрытыми сущностями."""
    return html.unescape(re.sub(r'<[^>]+>', '', text))


@pytest.mark.asyncio
async def test_status_keeps_a_reminder_of_the_task(monkeypatch):
    """Подсказку с описанием сменяет статус — название и описание переезжают в него."""
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot)
        await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)
        await _drain_status()

    kwargs = bot.send_message.await_args.kwargs
    assert kwargs['parse_mode'] == 'HTML'
    assert kwargs['text'] == (
        '📝 Задание: <b>Лайки &lt;канала&gt;</b>\n'
        'Поставьте лайки всем постам &amp; пришлите скриншоты\n\n'
        '📸 Получено скриншотов: 1 из 3.\n'
        'Можно прислать ещё или нажать «Готово».'
    )
    bot.delete_message.assert_awaited_once_with(chat_id=TG_ID, message_id=PROMPT_MESSAGE_ID)


@pytest.mark.asyncio
async def test_long_description_is_whole_in_prompt_and_short_in_status(monkeypatch):
    async with _db(monkeypatch, photo_task={'description': LONG_DESCRIPTION}) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        callback = await _enter_report(db, user, state, bot)
        await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)
        await _drain_status()

    prompt = callback.message.edit_text.await_args.args[0]
    assert html.escape(LONG_DESCRIPTION) in prompt  # целиком и с переносами строк
    reminder = bot.send_message.await_args.kwargs['text'].split('\n')[1]
    assert reminder.startswith('1. Подпишитесь на канал &lt;LEDOKOL&gt;. 2. Поставьте')
    assert reminder.endswith('…')
    assert len(_visible(reminder)) == handlers.STATUS_EXCERPT_LIMIT


@pytest.mark.asyncio
async def test_task_without_description_has_no_empty_blocks(monkeypatch):
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        callback = await _enter_report(db, user, state, bot, task_id=SECOND_PHOTO_TASK_ID)
        await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)
        await _drain_status()

    prompt = callback.message.edit_text.await_args.args[0]
    assert prompt.startswith('📸 <b>Отчёт: Отзыв</b>\n\nПришлите скриншоты (до 1 шт.) — по одному или альбомом')
    status = bot.send_message.await_args.kwargs['text']
    assert status.startswith('📝 Задание: <b>Отзыв</b>\n\n📸 Получено скриншотов: 1 из 1.')


@pytest.mark.asyncio
async def test_status_of_a_collection_started_before_update(monkeypatch):
    """Сбор, начатый до обновления бота: в state нет названия — статус без напоминания."""
    state = _state()
    await state.set_state(RewardTaskStates.waiting_for_photos)
    await state.set_data({'reward_task_id': PHOTO_TASK_ID, 'reward_task_max_photos': 3, 'reward_task_photos': []})
    bot = _bot()

    _visible_without_db(monkeypatch)

    await _raw(handlers.remind_photo_only)(
        _text_message(bot, 'отчёт'), SimpleNamespace(id=USER_ID, language='ru'), state, None
    )
    await _drain_status()

    assert bot.send_message.await_args.kwargs['text'].startswith('📸 Получено скриншотов: 0 из 3.')


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'fa', 'zh'])
def test_longest_texts_fit_a_telegram_message(language):
    """Описание не обрезается: самое длинное задание и причина отказа влезают в
    сообщение (4096). В подпись к фото (1024) влезают не всегда — тогда экран и
    подсказка уходят текстом (фоллбек edit_or_answer_photo и патча edit_text)."""
    texts = get_texts(language)
    task = SimpleNamespace(
        id=PHOTO_TASK_ID,
        kind='photo_report',
        title='Т' * service.TITLE_MAX_LENGTH,
        description='<&>' * (service.DESCRIPTION_MAX_LENGTH // 3),
        reward_days=service.REWARD_DAYS_MAX,
        max_photos=service.MAX_PHOTOS_LIMIT,
    )
    rejected = SimpleNamespace(task=task, status='rejected', reject_reason='П' * service.MAX_REJECT_REASON_LENGTH)
    limit = service.MAX_PHOTOS_LIMIT

    prompt = handlers._photo_prompt_text(texts, task, limit, limit)
    detail = handlers._detail_text(texts, rejected)

    assert html.escape(task.description) in prompt
    assert len(_visible(prompt)) <= 4096
    assert len(_visible(detail)) <= 4096


# ── Отключённая подписка, ограничение, нет подписки ──────────────────────────

SUPPORT_URL = 'https://t.me/ledokol_support'
SUPPORT_LAYOUT = [('🆘 Написать в поддержку', None, SUPPORT_URL), ('⬅️ Назад', 'back_to_menu', None)]
# Раздел виден, но выполнять задания нельзя — объяснение. Отключённая подписка
# сюда больше не относится: такому пользователю раздел не показывается (ниже).
INELIGIBLE = {
    'restricted': (
        {'restricted': True},
        'Продление подписки для вашего аккаунта ограничено. Если это ошибка — напишите в поддержку.',
    ),
    'no_subscription': (
        {'subscription_status': None},
        'Сначала активируйте пробный период или оформите подписку, затем возвращайтесь к заданиям.',
    ),
}


async def _press_entry(entry: str, db, user, state, bot) -> MagicMock:
    if entry == 'list':
        callback = _callback('reward_tasks_menu', bot)
        await _raw(handlers.show_reward_tasks)(callback, user, db, state)
    elif entry == 'task':
        callback = _callback('reward_task:100', bot)
        await _raw(handlers.show_reward_task)(callback, user, db, state)
    elif entry == 'boost':
        callback = _callback('reward_task_boost:101', bot)
        await _raw(handlers.check_reward_task_boost)(callback, user, db, state)
    else:
        callback = await _enter_report(db, user, state, bot)
    return callback


@pytest.mark.parametrize('entry', ['list', 'task', 'boost', 'photo'])
@pytest.mark.parametrize('reason', list(INELIGIBLE))
@pytest.mark.asyncio
async def test_ineligible_user_sees_why_on_every_entry(monkeypatch, screens, panel, lava, rate_limit, reason, entry):
    """Список, экран задания, «Проверить буст» и «Отправить отчёт» без права на
    задания ведут на один экран: ограничение — в поддержку (без списка заданий),
    нет подписки — в триал/покупку с превью."""
    monkeypatch.setattr(type(settings), 'get_support_contact_url', lambda self: SUPPORT_URL)
    db_options, message = INELIGIBLE[reason]
    async with _db(monkeypatch, **db_options) as db:
        user = await _load_user(db)
        state = _state()
        bot = _boost_bot(datetime.now(UTC) + timedelta(days=20))

        callback = await _press_entry(entry, db, user, state, bot)

        assert await _submissions(db) == []
    text, keyboard = _screen(screens)
    if reason == 'no_subscription':
        assert text.startswith(f'🎁 <b>Задания</b>\n\n{message}\n\nЗадания, которые станут доступны:')
        assert _callbacks(keyboard) == ['menu_trial', 'menu_buy', 'back_to_menu']
    else:
        assert text == f'🎁 <b>Задания</b>\n\n{message}'  # без превью: выполнить всё равно нельзя
        assert _buttons(keyboard) == SUPPORT_LAYOUT  # ни триала, ни покупки
    callback.answer.assert_awaited_once_with()  # экран, а не алерт
    bot.get_user_chat_boosts.assert_not_awaited()
    assert panel.calls == []
    assert await state.get_state() is None


# ── Скрытый раздел: отключённая подписка, режим «только админы» ─────────────

UNAVAILABLE_ALERT = 'Раздел недоступен'
STALE_STATUS_MESSAGE_ID = 77
# Ответ владельца 2026-10-09: раздел скрыт, а не объяснён.
HIDDEN = {
    # Любая DISABLED показывается пользователю как «Отключена» — с остатком срока…
    'disabled': {'db': {'subscription_status': 'disabled'}, 'for_all': True},
    # …и с вышедшим (до 2026-10-09 такая оживала наградой).
    'disabled_lapsed': {'db': {'subscription_status': 'disabled', 'subscription_days': -5}, 'for_all': True},
    # REWARD_TASKS_FOR_ALL_USERS выключен, пользователь не админ (подписка активна).
    'admins_only': {'db': {}, 'for_all': False},
}


async def _stale_photo_flow(state: FSMContext) -> None:
    """Сбор скриншотов, начатый, пока раздел был виден (статус с «Готово»/«Отмена» в чате)."""
    await state.set_state(RewardTaskStates.waiting_for_photos)
    await state.set_data(
        {
            'reward_task_id': PHOTO_TASK_ID,
            'reward_task_title': 'Лайки <канала>',
            'reward_task_max_photos': 3,
            'reward_task_photos': [{'type': 'photo', 'file_id': _file_id('stale'), 'file_unique_id': 'AQADstale'}],
            'reward_task_status_chat_id': TG_ID,
            'reward_task_status_message_id': STALE_STATUS_MESSAGE_ID,
        }
    )


async def _press_hidden(entry: str, db, user, state, bot) -> MagicMock:
    """Кнопки раздела: входы (список, задание, буст, отчёт) и кнопки сбора отчёта."""
    if entry == 'done':
        callback = _callback('reward_task_done:100', bot)
        await _raw(handlers.submit_reward_task_report)(callback, user, db, state)
        return callback
    if entry == 'cancel':
        callback = _callback('reward_task_cancel:100', bot)
        await _raw(handlers.cancel_reward_task_report)(callback, user, db, state)
        return callback
    return await _press_entry(entry, db, user, state, bot)


@pytest.mark.parametrize('entry', ['list', 'task', 'boost', 'photo', 'done', 'cancel'])
@pytest.mark.parametrize('hidden', list(HIDDEN))
@pytest.mark.asyncio
async def test_hidden_section_answers_every_button_with_a_neutral_alert(
    monkeypatch, screens, panel, lava, rate_limit, hidden, entry
):
    """Старые кнопки (меню, экран задания, кнопки под уведомлениями, «Проверить буст»,
    сбор отчёта) скрытому разделу отвечают коротким «Раздел недоступен» и больше ничего:
    ни экранов, ни проверки буста, ни отчётов. Зависший сбор отчёта сбрасывается."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', HIDDEN[hidden]['for_all'])
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    async with _db(monkeypatch, **HIDDEN[hidden]['db']) as db:
        user = await _load_user(db)
        state = _state()
        await _stale_photo_flow(state)
        bot = _boost_bot(datetime.now(UTC) + timedelta(days=20))
        bot.send_message = AsyncMock()
        bot.delete_message = AsyncMock()

        callback = await _press_hidden(entry, db, user, state, bot)

        assert await _submissions(db) == []
    callback.answer.assert_awaited_once_with(UNAVAILABLE_ALERT, show_alert=True)
    screens.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    bot.get_user_chat_boosts.assert_not_awaited()
    assert panel.calls == []
    assert await state.get_state() is None
    assert await state.get_data() == {}
    bot.delete_message.assert_awaited_once_with(chat_id=TG_ID, message_id=STALE_STATUS_MESSAGE_ID)


HIDDEN_BUTTONS = {
    'reward_task:': handlers.show_reward_task,
    'reward_task_boost:': handlers.check_reward_task_boost,
    'reward_task_photo:': handlers.start_reward_task_report,
    'reward_task_done:': handlers.submit_reward_task_report,
    'reward_task_cancel:': handlers.cancel_reward_task_report,
}


@pytest.mark.parametrize(
    'data',
    [
        # Задания нет / выключено: «Задание не найдено» выдало бы, что раздел есть.
        'reward_task:999',
        f'reward_task:{INACTIVE_TASK_ID}',
        'reward_task_boost:999',
        f'reward_task_photo:{INACTIVE_TASK_ID}',
        'reward_task_done:999',
        'reward_task_cancel:999',
        # Буст уже засчитан: «Вы уже выполнили это задание» — тоже нет.
        f'reward_task_boost:{BOOST_TASK_ID}',
        f'reward_task_photo:{BOOST_TASK_ID}',
    ],
)
@pytest.mark.asyncio
async def test_hidden_section_does_not_reveal_task_details(monkeypatch, screens, panel, lava, rate_limit, data):
    """Скрытому разделу — нейтральный алерт раньше любых проверок задания."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', False)
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    handler = HIDDEN_BUTTONS[data[: data.index(':') + 1]]
    async with _db(monkeypatch) as db:
        await _add_submission(db, BOOST_TASK_ID, 'approved')
        user = await _load_user(db)
        bot = _boost_bot(datetime.now(UTC) + timedelta(days=20))
        callback = _callback(data, bot)

        await _raw(handler)(callback, user, db, _state())

    callback.answer.assert_awaited_once_with(UNAVAILABLE_ALERT, show_alert=True)
    screens.assert_not_awaited()
    callback.message.edit_text.assert_not_awaited()
    bot.get_user_chat_boosts.assert_not_awaited()
    assert panel.calls == []


@pytest.mark.parametrize('hidden', list(HIDDEN))
@pytest.mark.asyncio
async def test_hidden_section_drops_the_photo_flow_with_one_answer(monkeypatch, hidden):
    """Фото и не-фото в зависшем сборе: один нейтральный ответ, сбор сброшен, отчёта нет."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', HIDDEN[hidden]['for_all'])
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    async with _db(monkeypatch, **HIDDEN[hidden]['db']) as db:
        user = await _load_user(db)
        state = _state()
        await _stale_photo_flow(state)
        bot = _bot()
        first, second = _photo_message(bot), _photo_message(bot)
        text = _text_message(bot, 'вот отчёт')

        await _raw(handlers.collect_reward_task_photo)(first, user, state, db)
        await _raw(handlers.collect_reward_task_photo)(second, user, state, db)
        await _raw(handlers.remind_photo_only)(text, user, state, db)
        await _drain_status()

        assert await _submissions(db) == []
    first.answer.assert_awaited_once_with(UNAVAILABLE_ALERT)
    second.answer.assert_not_awaited()  # сбор уже сброшен — молчим
    text.answer.assert_not_awaited()
    bot.send_message.assert_not_awaited()  # статуса сбора больше нет
    bot.delete_message.assert_awaited_once_with(chat_id=TG_ID, message_id=STALE_STATUS_MESSAGE_ID)
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_hidden_section_album_gets_one_answer(monkeypatch):
    """Альбом в зависшем сборе: апдейты идут параллельно, ответ — один (замок на пользователя)."""
    monkeypatch.setattr(
        service,
        'get_reward_tasks_access',
        AsyncMock(return_value=service.RewardTasksAccess(visible=False, hidden_reason='subscription_disabled')),
    )
    state = _state(_SlowStorage())
    await _stale_photo_flow(state)
    bot = _bot()
    album = [_photo_message(bot, media_group_id='album-1') for _ in range(3)]
    user = SimpleNamespace(id=USER_ID, language='ru')

    await asyncio.gather(*(_raw(handlers.collect_reward_task_photo)(m, user, state, None) for m in album))

    assert sum(message.answer.await_count for message in album) == 1
    bot.delete_message.assert_awaited_once_with(chat_id=TG_ID, message_id=STALE_STATUS_MESSAGE_ID)
    assert await state.get_state() is None


@pytest.mark.parametrize('hidden', list(HIDDEN))
@pytest.mark.asyncio
async def test_hidden_section_tasks_command_is_a_neutral_answer(monkeypatch, hidden):
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', HIDDEN[hidden]['for_all'])
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    async with _db(monkeypatch, **HIDDEN[hidden]['db']) as db:
        user = await _load_user(db)
        state = _state()
        await _stale_photo_flow(state)
        bot = _bot()
        message = _text_message(bot, '/tasks')

        await _raw(handlers.cmd_tasks)(message, user, db, state)

    message.answer.assert_awaited_once_with(UNAVAILABLE_ALERT)
    assert await state.get_state() is None
    bot.delete_message.assert_awaited_once_with(chat_id=TG_ID, message_id=STALE_STATUS_MESSAGE_ID)


@pytest.mark.asyncio
async def test_hidden_section_keeps_other_fsm_flows(monkeypatch, screens):
    """Чужой сценарий (ввод промокода) скрытый раздел не трогает — сбрасывается только сбор отчёта."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', False)
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        await state.set_state(PromoCodeStates.waiting_for_code)
        callback = _callback('reward_tasks_menu', _bot())

        await _raw(handlers.show_reward_tasks)(callback, user, db, state)

    callback.answer.assert_awaited_once_with(UNAVAILABLE_ALERT, show_alert=True)
    assert await state.get_state() == PromoCodeStates.waiting_for_code.state


@pytest.mark.asyncio
async def test_admins_see_the_section_while_it_is_admins_only(monkeypatch, screens):
    """Переключатель выключен: админ (ADMIN_IDS) видит раздел, как раньше видели все."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', False)
    monkeypatch.setattr(settings, 'ADMIN_IDS', str(TG_ID))
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        callback = _callback('reward_tasks_menu', _bot())

        await _raw(handlers.show_reward_tasks)(callback, user, db, _state())

    text, keyboard = _screen(screens)
    assert text.startswith('🎁 <b>Задания</b>\n\nВыполняйте задания')
    assert 'reward_task:100' in _callbacks(keyboard)
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_done_after_subscription_was_disabled(monkeypatch, screens):
    """Подписку отключили посреди сбора отчёта: «Готово» — нейтральный алерт, сбор сброшен, отчёта нет.

    До 2026-10-09 здесь был экран «Ваша подписка сейчас отключена» с поддержкой.
    """
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        state = _state()
        bot = _bot()
        await _enter_report(db, user, state, bot)
        await _raw(handlers.collect_reward_task_photo)(_photo_message(bot), user, state, db)
        await _drain_status()
        status_message_id = (await state.get_data())['reward_task_status_message_id']
        await db.execute(update(Subscription).values(status='disabled'))
        await db.commit()
        callback = _callback('reward_task_done:100', bot, message_id=status_message_id)

        await _raw(handlers.submit_reward_task_report)(callback, user, db, state)

        assert await _submissions(db) == []
    assert await state.get_state() is None
    callback.answer.assert_awaited_once_with(UNAVAILABLE_ALERT, show_alert=True)
    screens.assert_not_awaited()
    bot.delete_message.assert_any_await(chat_id=TG_ID, message_id=status_message_id)


@pytest.mark.parametrize(
    ('code', 'alert'),
    [
        # До бота не доходит (заблокированного не пускает AuthMiddleware), но гонка
        # с блокировкой посреди начисления возможна
        (
            'user_blocked',
            'Ваш аккаунт заблокирован, поэтому награды за задания не начисляются. '
            'Если это ошибка — напишите в поддержку.',
        ),
        # Новый код сервиса, о котором бот ещё не знает
        ('something_new', 'Что-то пошло не так. Попробуйте позже.'),
        # Раздел скрылся посреди проверки (подписку отключили, переключатель выключили)
        ('tasks_unavailable', 'Раздел недоступен'),
        ('subscription_disabled', 'Раздел недоступен'),
    ],
)
@pytest.mark.asyncio
async def test_unexpected_error_code_is_an_alert(monkeypatch, screens, code, alert):
    monkeypatch.setattr(service, 'check_boost', AsyncMock(side_effect=service.RewardTaskError(code)))
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        callback = _callback('reward_task_boost:101', _bot())

        await _raw(handlers.check_reward_task_boost)(callback, user, db, _state())

    callback.answer.assert_awaited_once_with(alert, show_alert=True)
    screens.assert_not_awaited()


EXPIRED_QUERY = 'Bad Request: query is too old and response timeout expired or query ID is invalid'
PANEL_SYNC_FAILED_TEXT = 'Не удалось начислить дни: сервис подписок временно недоступен. Попробуйте позже.'


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=AnswerCallbackQuery(callback_query_id='1'), message=message)


@pytest.mark.asyncio
async def test_boost_failure_after_a_slow_grant_is_sent_as_a_message(monkeypatch, screens):
    """Начисление ждало панель (до ~13 с), и Telegram уже не принимает алерт на это нажатие.

    Без запасного пути пользователь не получил бы ничего: ни алерта, ни экрана.
    """
    monkeypatch.setattr(service, 'check_boost', AsyncMock(side_effect=service.RewardTaskError('panel_sync_failed')))
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        bot = _bot()
        callback = _callback('reward_task_boost:101', bot)
        callback.answer = AsyncMock(side_effect=_bad_request(EXPIRED_QUERY))

        await _raw(handlers.check_reward_task_boost)(callback, user, db, _state())

    callback.answer.assert_awaited_once_with(PANEL_SYNC_FAILED_TEXT, show_alert=True)
    bot.send_message.assert_awaited_once_with(chat_id=TG_ID, text=PANEL_SYNC_FAILED_TEXT, parse_mode=None)
    screens.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_already_completed_answer_still_refreshes_the_task_screen(monkeypatch, screens):
    """Опоздавший ответ на «выполнено» тоже уходит сообщением, а экран задания обновляется, как обычно."""
    monkeypatch.setattr(service, 'check_boost', AsyncMock(side_effect=service.RewardTaskError('already_completed')))
    async with _db(monkeypatch) as db:
        await _add_submission(db, BOOST_TASK_ID, 'approved')
        user = await _load_user(db)
        bot = _bot()
        callback = _callback('reward_task_boost:101', bot)
        callback.answer = AsyncMock(side_effect=_bad_request(EXPIRED_QUERY))

        await _raw(handlers.check_reward_task_boost)(callback, user, db, _state())

    text = 'Вы уже выполнили это задание — награда начислена.'
    bot.send_message.assert_awaited_once_with(chat_id=TG_ID, text=text, parse_mode=None)
    screen_text, _ = _screen(screens)
    assert 'Задание выполнено' in screen_text


@pytest.mark.asyncio
async def test_only_an_expired_query_falls_back_to_a_message(monkeypatch, screens):
    """Другая ошибка Telegram — не «опоздали»: её разбирает error_handler, как раньше."""
    monkeypatch.setattr(service, 'check_boost', AsyncMock(side_effect=service.RewardTaskError('panel_sync_failed')))
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        bot = _bot()
        callback = _callback('reward_task_boost:101', bot)
        callback.answer = AsyncMock(side_effect=_bad_request('Bad Request: MESSAGE_TOO_LONG'))

        with pytest.raises(TelegramBadRequest):
            await _raw(handlers.check_reward_task_boost)(callback, user, db, _state())

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_fast_buttons_do_not_resend_an_expired_alert(monkeypatch, screens):
    """Остальные кнопки отвечают сразу — для них опоздание значит старый апдейт, сообщений не шлём."""
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        bot = _bot()
        callback = _callback(f'reward_task:{INACTIVE_TASK_ID}', bot)
        callback.answer = AsyncMock(side_effect=_bad_request(EXPIRED_QUERY))

        with pytest.raises(TelegramBadRequest):
            await _raw(handlers.show_reward_task)(callback, user, db, _state())

    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    ('language', 'message'),
    [
        ('en', 'Subscription renewal is restricted for your account. If you think this is a mistake, contact support.'),
        ('ua', 'Продовження підписки для вашого акаунта обмежено. Якщо це помилка — напишіть у підтримку.'),
    ],
)
@pytest.mark.asyncio
async def test_restricted_text_in_other_languages(monkeypatch, screens, language, message):
    monkeypatch.setattr(type(settings), 'get_support_contact_url', lambda self: SUPPORT_URL)
    async with _db(monkeypatch, restricted=True) as db:
        user = await _load_user(db)
        user.language = language
        await db.commit()
        callback = _callback('reward_tasks_menu', _bot())

        await _raw(handlers.show_reward_tasks)(callback, user, db, _state())

    text, _ = _screen(screens)
    assert text == f'{handlers._title(get_texts(language))}\n\n{message}'


def test_unknown_ineligible_reason_goes_to_support(monkeypatch):
    """Новая причина «нельзя» из сервиса: общий текст и поддержка, а не покупка."""
    monkeypatch.setattr(type(settings), 'get_support_contact_url', lambda self: SUPPORT_URL)
    texts = get_texts('ru')
    views = [SimpleNamespace(task=SimpleNamespace(title='Лайки', reward_days=1))]

    text = handlers._ineligible_text(texts, 'ru', 'something_new', views)
    keyboard = get_reward_tasks_ineligible_keyboard('ru', 'something_new', show_trial=True)

    assert text == '🎁 <b>Задания</b>\n\nЧто-то пошло не так. Попробуйте позже.'
    assert _buttons(keyboard) == SUPPORT_LAYOUT


# ── Кнопки под уведомлениями о проверке ──────────────────────────────────────

LOGO_FILE_ID = 'AgACAgIAAxkBAAIBlogo0000001'
NOTICE_MESSAGE_ID = 77
SCREEN_MESSAGE_ID = 78
REJECT_REASON = 'На скриншоте не видно лайков.'


class _TelegramApi:
    """Bot API без сети: сюда приходят методы aiogram, ответы — как у Telegram.

    Фото к текстовому сообщению Telegram не добавляет: editMessageMedia по
    тексту — ошибка (на ней общий фоллбек удаляет сообщение и шлёт новое).
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.sent: list[Message] = []
        self.text_message_ids: set[int] = set()
        self._ids = itertools.count(900)

    async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
        self.calls.append(method)
        if isinstance(method, EditMessageMedia):
            if method.message_id in self.text_message_ids:
                raise TelegramBadRequest(method=method, message='Bad Request: there is no media in the message to edit')
            return _tg_message(self, method.message_id, caption=method.media.caption, photo=True)
        if isinstance(method, EditMessageText):
            return _tg_message(self, method.message_id, text=method.text)
        if isinstance(method, (SendPhoto, SendMessage)):
            photo = isinstance(method, SendPhoto)
            message = _tg_message(
                self,
                next(self._ids),
                text=None if photo else method.text,
                caption=method.caption if photo else None,
                photo=photo,
                reply_markup=method.reply_markup,
            )
            self.sent.append(message)
            return message
        return True

    def of(self, *kinds: type) -> list[Any]:
        return [call for call in self.calls if isinstance(call, kinds)]

    def screens(self) -> list[Any]:
        return self.of(SendPhoto, SendMessage, EditMessageText, EditMessageMedia)


def _tg_message(
    api: _TelegramApi,
    message_id: int,
    *,
    text: str | None = None,
    caption: str | None = None,
    photo: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    if not photo:
        api.text_message_ids.add(message_id)
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=TG_ID, type='private'),
        from_user=TgUser(id=1, is_bot=True, first_name='LEDOKOL'),
        text=text,
        caption=caption,
        photo=[PhotoSize(file_id=LOGO_FILE_ID, file_unique_id='AQADlogo', width=640, height=640)] if photo else None,
        reply_markup=reply_markup,
    ).as_(api)


def _tg_callback(api: _TelegramApi, message: Message, data: str) -> CallbackQuery:
    return CallbackQuery(
        id='1',
        from_user=TgUser(id=TG_ID, is_bot=False, first_name='Alice', language_code='ru'),
        chat_instance='ci',
        data=data,
        message=message,
    ).as_(api)


def _text_of(call: Any) -> str:
    if isinstance(call, SendPhoto):
        return call.caption
    if isinstance(call, EditMessageMedia):
        return call.media.caption
    return call.text


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch, tmp_path) -> _TelegramApi:
    """Настоящие aiogram-сообщения поверх _TelegramApi, Message.answer/edit_text
    пропатчены, как в проде (app/bot.py → patch_message_methods), логотип — file_id."""
    logo = tmp_path / 'logo.png'
    logo.write_bytes(b'\x89PNG')
    monkeypatch.setattr(Message, 'answer', message_patch._answer_with_photo)
    monkeypatch.setattr(Message, 'edit_text', message_patch._edit_with_photo)
    for module in (message_patch, photo_message):
        monkeypatch.setattr(module, 'LOGO_PATH', logo)
        monkeypatch.setattr(module, 'get_logo_media', lambda: LOGO_FILE_ID)
    monkeypatch.setattr(message_patch, '_logo_file_id', None)
    return _TelegramApi()


async def _review_notice(api: _TelegramApi, db, action: str) -> Message:
    """Уведомление о проверке отчёта — ровно то, что шлёт сервис, — сообщением в чате."""
    await _add_submission(db, PHOTO_TASK_ID, 'pending')
    submission_id = (await db.execute(select(RewardTaskSubmission.id))).scalar_one()
    outbox = MagicMock()
    outbox.send_message = AsyncMock()
    if action == 'reject':
        results = await service.reject(db, [submission_id], None, REJECT_REASON, bot=outbox)
    else:
        results = await service.approve(db, [submission_id], None, bot=outbox)
    assert [result.ok for result in results] == [True]
    sent = outbox.send_message.await_args.kwargs
    return _tg_message(api, NOTICE_MESSAGE_ID, text=sent['text'], reply_markup=sent['reply_markup'])


@pytest.mark.parametrize('logo_mode', [True, False])
@pytest.mark.asyncio
async def test_resend_button_under_rejection_notice(monkeypatch, telegram, logo_mode):
    """«📸 Отправить новый отчёт» под уведомлением: экран задания с причиной отказа.

    С логотипом экран приходит новым фото, уведомление не трогается (раньше
    editMessageMedia по тексту падал, и фоллбек удалял уведомление). Без
    логотипа уведомление редактируется в экран задания — причина есть и на нём.
    """
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', logo_mode)
    async with _db(monkeypatch) as db:
        notice = await _review_notice(telegram, db, 'reject')
        assert _callbacks(notice.reply_markup) == ['reward_task:100']
        user = await _load_user(db)

        await _raw(handlers.show_reward_task)(_tg_callback(telegram, notice, 'reward_task:100'), user, db, _state())

    assert telegram.of(DeleteMessage) == []  # уведомление с причиной отказа не пропадает
    assert telegram.of(EditMessageMedia) == []  # и не превращается в фото
    [screen] = telegram.screens()
    assert f'❌ Отчёт отклонён: {REJECT_REASON}' in _text_of(screen)
    assert ('📸 Отправить новый отчёт', 'reward_task_photo:100', None) in _buttons(screen.reply_markup)
    if logo_mode:
        assert isinstance(screen, SendPhoto)
        assert screen.photo == LOGO_FILE_ID
        assert screen.parse_mode == 'HTML'
    else:
        assert isinstance(screen, EditMessageText)
        assert screen.message_id == NOTICE_MESSAGE_ID
    assert len(telegram.of(AnswerCallbackQuery)) == 1


@pytest.mark.parametrize('logo_mode', [True, False])
@pytest.mark.asyncio
async def test_tasks_button_under_approval_notice(monkeypatch, telegram, panel, lava, logo_mode):
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', logo_mode)
    async with _db(monkeypatch) as db:
        notice = await _review_notice(telegram, db, 'approve')
        assert _callbacks(notice.reply_markup) == ['reward_tasks_menu']
        user = await _load_user(db)

        await _raw(handlers.show_reward_tasks)(_tg_callback(telegram, notice, 'reward_tasks_menu'), user, db, _state())

    assert telegram.of(DeleteMessage) == []
    assert telegram.of(EditMessageMedia) == []
    [screen] = telegram.screens()
    assert 'Выполняйте задания' in _text_of(screen)
    assert ('✅ Лайки <канала> · +2 дн.', 'reward_task:100', None) in _buttons(screen.reply_markup)
    if logo_mode:
        assert isinstance(screen, SendPhoto)
    else:
        assert isinstance(screen, EditMessageText)
        assert screen.message_id == NOTICE_MESSAGE_ID


@pytest.mark.asyncio
async def test_logo_screen_is_still_edited_in_place(monkeypatch, telegram):
    """Контроль: обычный экран раздела (фото с логотипом, одна кнопка «К заданиям»)
    по-прежнему меняется на месте, а не дублируется новым сообщением."""
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
    back = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='⬅️ К заданиям', callback_data='reward_tasks_menu')]]
    )
    async with _db(monkeypatch) as db:
        user = await _load_user(db)
        detail = _tg_message(telegram, SCREEN_MESSAGE_ID, caption='✅ Лайки', photo=True, reply_markup=back)

        await _raw(handlers.show_reward_tasks)(_tg_callback(telegram, detail, 'reward_tasks_menu'), user, db, _state())

    [screen] = telegram.screens()
    assert isinstance(screen, EditMessageMedia)
    assert screen.message_id == SCREEN_MESSAGE_ID
    assert telegram.of(DeleteMessage) == []


@pytest.mark.parametrize('long_description', [False, True])
@pytest.mark.asyncio
async def test_prompt_on_logo_screen_respects_caption_limit(monkeypatch, telegram, long_description):
    """Подсказка с описанием на экране с логотипом: влезает в подпись (1024) —
    фото редактируется; не влезает — фото удаляется, подсказка целиком приходит
    текстом, и сбор следит уже за новым сообщением."""
    monkeypatch.setattr(settings, 'ENABLE_LOGO_MODE', True)
    description = LONG_DESCRIPTION if long_description else 'Поставьте реакцию на 5 последних постов.'
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text='📸 Отправить отчёт', callback_data='reward_task_photo:100')]]
    )
    async with _db(monkeypatch, photo_task={'description': description}) as db:
        user = await _load_user(db)
        state = _state()
        detail = _tg_message(telegram, SCREEN_MESSAGE_ID, caption='🎁 Лайки', photo=True, reply_markup=keyboard)

        await _raw(handlers.start_reward_task_report)(
            _tg_callback(telegram, detail, 'reward_task_photo:100'), user, db, state
        )
        data = await state.get_data()

    [prompt] = telegram.screens()
    assert html.escape(description) in _text_of(prompt)
    assert _callbacks(prompt.reply_markup) == ['reward_task_cancel:100']
    if long_description:
        assert len(_visible(_text_of(prompt))) > message_patch.TELEGRAM_CAPTION_LIMIT
        assert [call.message_id for call in telegram.of(DeleteMessage)] == [SCREEN_MESSAGE_ID]
        assert isinstance(prompt, SendMessage)
        assert data['reward_task_status_message_id'] == telegram.sent[-1].message_id
    else:
        assert isinstance(prompt, EditMessageMedia)
        assert prompt.message_id == SCREEN_MESSAGE_ID
        assert telegram.of(DeleteMessage) == []
        assert data['reward_task_status_message_id'] == SCREEN_MESSAGE_ID


# ── Уведомления о проверке автору, которому раздел скрыт ─────────────────────


async def _pending_submission_id(db) -> int:
    result = await db.execute(select(RewardTaskSubmission.id).where(RewardTaskSubmission.status == 'pending'))
    return result.scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize('author', [*HIDDEN, 'visible'])
async def test_rejection_notice_invites_a_new_report_only_if_the_author_sees_the_section(monkeypatch, author):
    """Раздел автору скрыт (ответ владельца 2026-10-09) — отказ без «можно отправить новый
    отчёт» и без кнопки: она ответила бы «Раздел недоступен»."""
    case = HIDDEN.get(author, {'db': {}, 'for_all': True})
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', case['for_all'])
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    async with _db(monkeypatch, **case['db']) as db:
        await _add_submission(db, PHOTO_TASK_ID, 'pending')
        submission_id = await _pending_submission_id(db)
        bot = _bot()

        [result] = await service.reject(db, [submission_id], None, 'Скриншоты <не те>', bot=bot)

    assert (result.ok, result.error_code) == (True, None)
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert 'Причина: Скриншоты &lt;не те&gt;' in kwargs['text']
    if author == 'visible':
        assert kwargs['text'].endswith('Можно отправить новый отчёт.')
        assert _callbacks(kwargs['reply_markup']) == [f'reward_task:{PHOTO_TASK_ID}']
    else:
        assert 'Можно отправить новый отчёт' not in kwargs['text']
        assert kwargs['text'].endswith('Причина: Скриншоты &lt;не те&gt;')
        assert 'reply_markup' not in kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize('for_all', [False, True], ids=['admins_only', 'visible'])
async def test_approval_notice_links_the_section_only_if_the_author_sees_it(
    monkeypatch, panel, lava, rate_limit, for_all
):
    """Режим «только админы» включили, пока отчёт ждал: дни начислены, уведомление
    приходит, но без кнопки «🎁 Задания»."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', for_all)
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    async with _db(monkeypatch) as db:
        await _add_submission(db, PHOTO_TASK_ID, 'pending')
        submission_id = await _pending_submission_id(db)
        bot = _bot()

        [result] = await service.approve(db, [submission_id], None, bot=bot)

    assert (result.ok, result.error_code) == (True, None)
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs['text'].startswith('✅')
    if for_all:
        assert _callbacks(kwargs['reply_markup']) == ['reward_tasks_menu']
    else:
        assert 'reply_markup' not in kwargs


@pytest.mark.asyncio
async def test_hidden_author_check_failure_keeps_the_usual_notice(monkeypatch):
    """Сбой проверки видимости — уведомление как раньше: оно важнее кнопки."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', True)
    monkeypatch.setattr(service, 'get_reward_tasks_access', AsyncMock(side_effect=RuntimeError('db down')))
    async with _db(monkeypatch) as db:
        await _add_submission(db, PHOTO_TASK_ID, 'pending')
        submission_id = await _pending_submission_id(db)
        bot = _bot()

        [result] = await service.reject(db, [submission_id], None, 'Не те скриншоты', bot=bot)

    assert result.ok
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs['text'].endswith('Можно отправить новый отчёт.')
    assert _callbacks(kwargs['reply_markup']) == [f'reward_task:{PHOTO_TASK_ID}']


# ── Скрытый раздел: ветки, которые ревью нашло непокрытыми ───────────────────


@pytest.mark.parametrize('hidden', list(HIDDEN))
@pytest.mark.asyncio
async def test_text_first_in_a_hidden_stale_photo_flow_gets_the_neutral_answer(monkeypatch, hidden):
    """Первое сообщение в оставшемся сборе — текст, а не фото: тот же нейтральный ответ,
    сбор сброшен, «пришлите фото» не показывается."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', HIDDEN[hidden]['for_all'])
    monkeypatch.setattr(settings, 'ADMIN_IDS', '')
    async with _db(monkeypatch, **HIDDEN[hidden]['db']) as db:
        user = await _load_user(db)
        state = _state()
        await _stale_photo_flow(state)
        bot = _bot()
        text = _text_message(bot, 'вот отчёт')

        await _raw(handlers.remind_photo_only)(text, user, state, db)
        await _drain_status()

        assert await _submissions(db) == []
    text.answer.assert_awaited_once_with(UNAVAILABLE_ALERT)
    bot.send_message.assert_not_awaited()
    bot.delete_message.assert_awaited_once_with(chat_id=TG_ID, message_id=STALE_STATUS_MESSAGE_ID)
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_section_hidden_between_entry_and_screen_answers_the_neutral_alert(monkeypatch, screens):
    """Вход видел раздел (без подписки — с объяснением), а к показу экрана подписку
    отключили: нейтральный ответ и сброс сбора, как у любого скрытого входа."""
    visible_ineligible = service.RewardTasksAccess(visible=True, ineligible_reason='no_subscription')
    hidden = service.RewardTasksAccess(visible=False, hidden_reason='subscription_disabled')
    monkeypatch.setattr(
        service, 'get_reward_tasks_access', AsyncMock(side_effect=[visible_ineligible, hidden, hidden, hidden])
    )
    monkeypatch.setattr(
        service,
        'get_task_view',
        AsyncMock(
            return_value=SimpleNamespace(
                task=SimpleNamespace(id=PHOTO_TASK_ID, kind='photo_report'), status='available'
            )
        ),
    )
    state = _state()
    await _stale_photo_flow(state)
    bot = _bot()
    bot.delete_message = AsyncMock()
    callback = _callback(f'reward_task_photo:{PHOTO_TASK_ID}', bot)
    user = SimpleNamespace(id=1, language='ru', telegram_id=TG_ID)

    await _raw(handlers.start_reward_task_report)(callback, user, None, state)

    callback.answer.assert_awaited_once_with(UNAVAILABLE_ALERT, show_alert=True)
    screens.assert_not_awaited()
    assert await state.get_state() is None
    bot.delete_message.assert_awaited_once_with(chat_id=TG_ID, message_id=STALE_STATUS_MESSAGE_ID)
