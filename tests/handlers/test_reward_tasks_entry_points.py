"""Входы в «Задания»: кнопка главного меню, /tasks, диплинк /start tasks.

А также то, что вокруг: маршрутизация апдейтов на хендлеры раздела, троттлинг
(альбом не должен теряться), схема конструктора меню и тексты во всех локалях.

Кнопка главного меню (оба пути: конструктор меню и обычная клавиатура) и
``/start tasks`` следуют ``get_reward_tasks_access``: скрытому разделу
(отключённая подписка, блокировка, режим «только админы») кнопки нет, а
диплинк открывает обычный старт. Переключатель REWARD_TASKS_FOR_ALL_USERS,
изменённый в админке бота, действует со следующего показа меню.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Document, Message, PhotoSize, User as TgUser

import app.handlers.start as start_module
from app.config import settings
from app.database.models import (
    AdminRole,
    RewardTask,
    RewardTaskSubmission,
    SentNotification,
    Subscription,
    SystemSetting,
    Tariff,
    TrafficPurchase,
    User,
    UserRole,
)
from app.handlers import reward_tasks as handlers
from app.keyboards.inline import get_main_menu_keyboard, get_main_menu_keyboard_async
from app.localization.texts import get_texts
from app.middlewares.button_stats import BUILTIN_CALLBACKS
from app.middlewares.throttling import ThrottlingMiddleware
from app.services import reward_task_service as service
from app.services.menu_layout import AVAILABLE_CALLBACKS, BUILTIN_BUTTONS_INFO, DEFAULT_MENU_CONFIG, MenuContext
from app.services.menu_layout.constants import REWARD_TASKS_MENU_ACTION
from app.services.menu_layout.service import MenuLayoutService
from app.states import PromoCodeStates, RewardTaskStates
from app.webapi.schemas.menu_layout import ButtonConditions, MenuLayoutConfig


TG_ID = 555
ROOT = Path(__file__).resolve().parents[2]
MENU_TABLES = [
    User.__table__,
    Tariff.__table__,
    Subscription.__table__,
    TrafficPurchase.__table__,
    SentNotification.__table__,
    RewardTask.__table__,
    RewardTaskSubmission.__table__,
    AdminRole.__table__,
    UserRole.__table__,
    SystemSetting.__table__,
]
LOCALES_DIR = ROOT / 'app' / 'localization' / 'locales'
LANGUAGES = ('ru', 'en', 'ua', 'fa', 'zh')


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=TG_ID, user_id=TG_ID))


def _callbacks(markup) -> list[str | None]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class _Stop(Exception):
    """Обрывает /start там, где проверка уже сделана, а дальше — регистрация."""


# ── Кнопка главного меню ─────────────────────────────────────────────────────


def test_default_menu_has_tasks_row_behind_condition() -> None:
    row = next(row for row in DEFAULT_MENU_CONFIG['rows'] if row['id'] == 'reward_tasks_row')
    assert row['buttons'] == ['reward_tasks']
    assert row['conditions'] == {'reward_tasks_visible': True}

    button = DEFAULT_MENU_CONFIG['buttons']['reward_tasks']
    assert button['type'] == 'builtin'
    assert button['action'] == 'reward_tasks_menu'
    assert button['text']['ru'] == '🎁 Задания'
    assert button['conditions'] == {'reward_tasks_visible': True}

    info = next(item for item in BUILTIN_BUTTONS_INFO if item['id'] == 'reward_tasks')
    assert info['callback_data'] == 'reward_tasks_menu'
    assert info['default_conditions'] == {'reward_tasks_visible': True}
    assert any(item['callback_data'] == 'reward_tasks_menu' for item in AVAILABLE_CALLBACKS)
    assert 'reward_tasks_menu' in BUILTIN_CALLBACKS


def test_condition_follows_context_flag() -> None:
    condition = {'reward_tasks_visible': True}
    assert MenuLayoutService._evaluate_conditions(condition, MenuContext(reward_tasks_visible=True)) is True
    assert MenuLayoutService._evaluate_conditions(condition, MenuContext(reward_tasks_visible=False)) is False


def test_menu_editor_schema_keeps_the_condition() -> None:
    """Сохранение меню через API (extra='ignore') не должно молча терять условие."""
    assert ButtonConditions(reward_tasks_visible=True).model_dump(exclude_none=True) == {'reward_tasks_visible': True}
    config = MenuLayoutConfig.model_validate(copy.deepcopy(DEFAULT_MENU_CONFIG))
    row = next(row for row in config.rows if row.id == 'reward_tasks_row')
    assert row.conditions.reward_tasks_visible is True
    assert config.buttons['reward_tasks'].conditions.reward_tasks_visible is True


_MENU_USER = SimpleNamespace(id=1, telegram_id=TG_ID, status='active')


@pytest.mark.parametrize('layout_enabled', [True, False])
@pytest.mark.parametrize(('has_tasks', 'visible'), [(True, True), (True, False), (False, True)])
@pytest.mark.asyncio
async def test_main_menu_shows_tasks_button_only_with_active_tasks_and_visible_section(
    monkeypatch, layout_enabled, has_tasks, visible
) -> None:
    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', layout_enabled)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')
    monkeypatch.setattr(MenuLayoutService, 'get_config', AsyncMock(return_value=copy.deepcopy(DEFAULT_MENU_CONFIG)))
    has_active_tasks = AsyncMock(return_value=has_tasks)
    monkeypatch.setattr(service, 'has_active_tasks', has_active_tasks)
    access = AsyncMock(return_value=service.RewardTasksAccess(visible=visible))
    monkeypatch.setattr(service, 'get_reward_tasks_access', access)
    handler_db = object()

    keyboard = await get_main_menu_keyboard_async(db=handler_db, language='ru', user=_MENU_USER)

    assert ('reward_tasks_menu' in _callbacks(keyboard)) is (has_tasks and visible)
    # Проверка идёт в своей сессии, а не в транзакции хендлера
    assert has_active_tasks.await_args.args[0] is not handler_db
    if has_tasks:
        assert access.await_args.args[0] is has_active_tasks.await_args.args[0]
        assert access.await_args.args[1] is _MENU_USER
    else:
        access.assert_not_awaited()  # без заданий — ни одного запроса про пользователя


@pytest.mark.asyncio
async def test_menu_without_a_user_has_no_tasks_button(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', False)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')
    monkeypatch.setattr(service, 'has_active_tasks', AsyncMock(return_value=True))

    keyboard = await get_main_menu_keyboard_async(db=object(), language='ru')

    assert 'reward_tasks_menu' not in _callbacks(keyboard)


@pytest.mark.asyncio
async def test_failed_visibility_check_hides_button_but_menu_works(monkeypatch) -> None:
    import app.database.database as database_module

    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', False)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')
    monkeypatch.setattr(database_module, 'AsyncSessionLocal', MagicMock(side_effect=RuntimeError('db down')))

    keyboard = await get_main_menu_keyboard_async(db=object(), language='ru', user=_MENU_USER)

    assert 'reward_tasks_menu' not in _callbacks(keyboard)
    assert 'menu_balance' in _callbacks(keyboard)


@pytest.mark.asyncio
async def test_failed_access_query_hides_button_but_menu_works(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', True)
    monkeypatch.setattr(MenuLayoutService, 'get_config', AsyncMock(return_value=copy.deepcopy(DEFAULT_MENU_CONFIG)))
    monkeypatch.setattr(service, 'has_active_tasks', AsyncMock(return_value=True))
    monkeypatch.setattr(service, 'get_reward_tasks_access', AsyncMock(side_effect=RuntimeError('db down')))

    keyboard = await get_main_menu_keyboard_async(db=object(), language='ru', user=_MENU_USER)

    assert 'reward_tasks_menu' not in _callbacks(keyboard)
    assert keyboard.inline_keyboard


def test_layout_action_constant_matches_the_service() -> None:
    assert REWARD_TASKS_MENU_ACTION == service.REWARD_TASKS_MENU_CALLBACK


@pytest.mark.parametrize('visible', [True, False])
@pytest.mark.asyncio
async def test_saved_layout_without_the_condition_still_hides_tasks_buttons(monkeypatch, visible) -> None:
    """В сохранённой раскладке у кнопки «Задания» могли убрать условие reward_tasks_visible
    (или завести свою кнопку с тем же callback): скрытому разделу её всё равно нет."""
    config = copy.deepcopy(DEFAULT_MENU_CONFIG)
    config['buttons']['reward_tasks']['conditions'] = None
    row = next(row for row in config['rows'] if row['id'] == 'reward_tasks_row')
    row['conditions'] = None
    row['buttons'].append('custom_tasks')
    config['buttons']['custom_tasks'] = {
        'type': 'callback',
        'text': {'ru': '🎯 Задания за дни'},
        'action': 'reward_tasks_menu',
        'enabled': True,
        'visibility': 'all',
        'conditions': None,
    }
    monkeypatch.setattr(MenuLayoutService, 'get_config', AsyncMock(return_value=config))

    keyboard = await MenuLayoutService.build_keyboard(
        object(), MenuContext(language='ru', reward_tasks_visible=visible)
    )

    assert _callbacks(keyboard).count('reward_tasks_menu') == (2 if visible else 0)
    assert 'menu_balance' in _callbacks(keyboard)


# ── Кнопка меню на настоящей видимости (SQLite) ──────────────────────────────

ADMIN_TG_ID = 9001


@contextlib.asynccontextmanager
async def _menu_world(
    monkeypatch, *, subscription: tuple[str, int] | None = ('active', 20), user_status: str = 'active', tg_id=TG_ID
):
    """Пользователь, одна подписка и включённое задание; меню берёт свою сессию к той же БД."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.database.database as database_module
    from tests.fixtures.sqlite_memory import memory_session

    async with memory_session(monkeypatch, MENU_TABLES) as db:
        now = datetime.now(UTC)
        db.add(User(id=1, telegram_id=tg_id, first_name='Alice', language='ru', status=user_status))
        db.add(RewardTask(id=1, kind='photo_report', title='Лайки', reward_days=1, created_at=now, updated_at=now))
        if subscription is not None:
            status, days = subscription
            db.add(
                Subscription(
                    id=10,
                    user_id=1,
                    status=status,
                    is_trial=False,
                    end_date=now + timedelta(days=days),
                    traffic_limit_gb=0,
                    remnawave_short_id='s10',
                )
            )
        await db.commit()
        monkeypatch.setattr(database_module, 'AsyncSessionLocal', async_sessionmaker(db.bind, expire_on_commit=False))
        service.invalidate_active_tasks_cache()
        try:
            yield db
        finally:
            service.invalidate_active_tasks_cache()


async def _menu_callbacks(db, *, layout_enabled: bool) -> list[str | None]:
    user = await db.get(User, 1)
    keyboard = await get_main_menu_keyboard_async(db=db, language='ru', user=user)
    return _callbacks(keyboard)


@pytest.mark.parametrize('layout_enabled', [True, False], ids=['menu_layout', 'classic'])
@pytest.mark.parametrize(
    ('case', 'world', 'switch_on', 'expected'),
    [
        ('active', {}, True, True),
        ('no_subscription', {'subscription': None}, True, True),  # виден с объяснением
        ('expired', {'subscription': ('expired', -3)}, True, True),
        ('disabled', {'subscription': ('disabled', 10)}, True, False),
        ('disabled_lapsed', {'subscription': ('disabled', -10)}, True, False),
        ('blocked', {'user_status': 'blocked'}, True, False),
        ('admins_only_user', {}, False, False),
        ('admins_only_admin', {'tg_id': ADMIN_TG_ID}, False, True),
        ('admins_only_admin_disabled', {'tg_id': ADMIN_TG_ID, 'subscription': ('disabled', 10)}, False, False),
    ],
)
@pytest.mark.asyncio
async def test_menu_button_follows_the_section_visibility(
    monkeypatch, layout_enabled, case, world, switch_on, expected
) -> None:
    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', layout_enabled)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', switch_on)
    monkeypatch.setattr(settings, 'ADMIN_IDS', str(ADMIN_TG_ID))
    monkeypatch.setattr(MenuLayoutService, 'get_config', AsyncMock(return_value=copy.deepcopy(DEFAULT_MENU_CONFIG)))
    async with _menu_world(monkeypatch, **world) as db:
        callbacks = await _menu_callbacks(db, layout_enabled=layout_enabled)

    assert ('reward_tasks_menu' in callbacks) is expected


@pytest.mark.asyncio
async def test_switch_toggled_in_bot_settings_changes_the_menu_at_once(monkeypatch) -> None:
    """«🔁 Переключить» в «🧩 Конфигурация бота» → «🎁 Задания»: следующее же меню — с кнопкой.

    Настоящий хендлер (с admin_required) и BotConfigurationService; кэш «есть ли
    задания» прогрет — переключатель он не прячет.
    """
    from aiogram.types import CallbackQuery as TgCallbackQuery

    from app.handlers.admin import bot_configuration as ui
    from app.services.system_settings_service import BotConfigurationService, bot_configuration_service

    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', True)
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', False)
    monkeypatch.setattr(settings, 'ADMIN_IDS', str(ADMIN_TG_ID))
    monkeypatch.setattr(BotConfigurationService, '_overrides_raw', {})
    monkeypatch.setattr(MenuLayoutService, 'get_config', AsyncMock(return_value=copy.deepcopy(DEFAULT_MENU_CONFIG)))
    token = bot_configuration_service.get_callback_token('REWARD_TASKS_FOR_ALL_USERS')

    async def press_toggle(db) -> MagicMock:
        callback = MagicMock(spec=TgCallbackQuery)
        callback.data = f'botcfg_toggle:reward_tasks:1:1:{token}'
        callback.from_user = SimpleNamespace(id=ADMIN_TG_ID)
        callback.message = MagicMock()
        callback.message.edit_text = AsyncMock()
        callback.answer = AsyncMock()
        await ui.toggle_setting(callback, db_user=SimpleNamespace(id=2, language='ru'), db=db, state=_state())
        callback.answer.assert_awaited_once_with('Обновлено')
        return callback

    async with _menu_world(monkeypatch) as db:
        assert 'reward_tasks_menu' not in await _menu_callbacks(db, layout_enabled=True)

        callback = await press_toggle(db)
        assert settings.REWARD_TASKS_FOR_ALL_USERS is True
        text = callback.message.edit_text.await_args.args[0]
        assert '<code>REWARD_TASKS_FOR_ALL_USERS</code>' in text
        assert '📁 Категория: 🎁 Задания' in text
        assert 'reward_tasks_menu' in await _menu_callbacks(db, layout_enabled=True)

        await press_toggle(db)
        assert settings.REWARD_TASKS_FOR_ALL_USERS is False
        assert 'reward_tasks_menu' not in await _menu_callbacks(db, layout_enabled=True)


def test_fallback_menu_keeps_donate_row_last(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True)
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'default')

    hidden = get_main_menu_keyboard(language='ru')
    shown = get_main_menu_keyboard(language='ru', show_reward_tasks=True)

    assert 'reward_tasks_menu' not in _callbacks(hidden)
    assert 'reward_tasks_menu' in _callbacks(shown)
    assert shown.inline_keyboard[-1][0].callback_data == 'menu_donate'


# ── Маршрутизация ────────────────────────────────────────────────────────────

_TG_USER = TgUser(id=TG_ID, is_bot=False, first_name='Alice')
_CHAT = Chat(id=TG_ID, type='private')


def _message(**fields) -> Message:
    return Message(message_id=1, date=datetime.now(UTC), chat=_CHAT, from_user=_TG_USER, **fields)


async def _route(observer, event, raw_state: str | None) -> str | None:
    """Имя первого хендлера раздела, чьи фильтры пропускают апдейт (как в aiogram)."""
    for handler in observer.handlers:
        passed, _ = await handler.check(event, raw_state=raw_state, bot=None)
        if passed:
            return handler.callback.__name__
    return None


@pytest.fixture(scope='module')
def dispatcher() -> Dispatcher:
    dp = Dispatcher()
    handlers.register_handlers(dp)
    return dp


@pytest.mark.parametrize(
    ('data', 'expected'),
    [
        ('reward_tasks_menu', 'show_reward_tasks'),
        ('reward_task:7', 'show_reward_task'),
        ('reward_task_boost:7', 'check_reward_task_boost'),
        ('reward_task_photo:7', 'start_reward_task_report'),
        ('reward_task_done:7', 'submit_reward_task_report'),
        ('reward_task_cancel:7', 'cancel_reward_task_report'),
        ('contests_menu', None),
    ],
)
@pytest.mark.asyncio
async def test_callbacks_reach_their_handlers(dispatcher, data, expected) -> None:
    query = CallbackQuery(id='1', from_user=_TG_USER, chat_instance='ci', data=data)
    assert await _route(dispatcher.callback_query, query, None) == expected


_COLLECTING = RewardTaskStates.waiting_for_photos.state
_PHOTO = [PhotoSize(file_id='AgACAgIAAxkBAAIBQ2Zx', file_unique_id='AQADxx', width=1280, height=720)]


@pytest.mark.parametrize(
    ('fields', 'raw_state', 'expected'),
    [
        ({'text': '/tasks'}, None, 'cmd_tasks'),
        # /tasks посреди сбора — список, а не подсказка «пришлите фото»
        ({'text': '/tasks'}, _COLLECTING, 'cmd_tasks'),
        ({'photo': _PHOTO}, _COLLECTING, 'collect_reward_task_photo'),
        ({'photo': _PHOTO, 'media_group_id': 'album'}, _COLLECTING, 'collect_reward_task_photo'),
        ({'text': 'вот отчёт'}, _COLLECTING, 'remind_photo_only'),
        (
            {'document': Document(file_id='BQACAgIAAxkBAAIBQ2Zx', file_unique_id='AgADyy', mime_type='image/png')},
            _COLLECTING,
            'remind_photo_only',
        ),
        # Вне сбора фото — не наше дело (его ловит общий «не понимаю»)
        ({'photo': _PHOTO}, None, None),
        ({'photo': _PHOTO}, PromoCodeStates.waiting_for_code.state, None),
    ],
)
@pytest.mark.asyncio
async def test_messages_reach_their_handlers(dispatcher, fields, raw_state, expected) -> None:
    assert await _route(dispatcher.message, _message(**fields), raw_state) == expected


def test_bot_registers_the_router() -> None:
    # app.bot не импортировать: в тестах redis.asyncio — заглушка
    tree = ast.parse((ROOT / 'app' / 'bot.py').read_text(encoding='utf-8'))
    setup = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'setup_bot')
    calls = {ast.unparse(node.func) for node in ast.walk(setup) if isinstance(node, ast.Call)}
    assert 'reward_tasks.register_handlers' in calls


# ── /start tasks ─────────────────────────────────────────────────────────────


def _start_message(text: str) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.from_user = SimpleNamespace(
        id=TG_ID, username='alice', first_name='Alice', last_name=None, language_code='ru'
    )
    message.answer = AsyncMock()
    message.bot = MagicMock()
    return message


@pytest.fixture
def start_deps(monkeypatch) -> SimpleNamespace:
    """Всё, что /start мог бы сделать с параметром как с кампанией или рефкодом."""
    deps = SimpleNamespace(
        opened=AsyncMock(),
        campaign_lookup=AsyncMock(return_value=None),
        referral_lookup=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(start_module, 'open_reward_tasks_message', deps.opened)
    monkeypatch.setattr(start_module, 'get_campaign_by_start_parameter', deps.campaign_lookup)
    monkeypatch.setattr(start_module, 'get_user_by_referral_code', deps.referral_lookup)
    monkeypatch.setattr(start_module, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    return deps


@pytest.mark.asyncio
async def test_start_tasks_opens_tasks_for_registered_user(start_deps) -> None:
    state = _state()
    user = SimpleNamespace(id=1, telegram_id=TG_ID, status='active', referred_by_id=None)
    message = _start_message('/start tasks')
    db = MagicMock()

    await start_module.cmd_start(message, state, db, db_user=user)

    start_deps.opened.assert_awaited_once_with(message, user, db, state)
    start_deps.campaign_lookup.assert_not_awaited()
    start_deps.referral_lookup.assert_not_awaited()
    assert 'referral_code' not in await state.get_data()


@pytest.mark.asyncio
async def test_start_tasks_for_new_user_is_plain_registration(start_deps, monkeypatch) -> None:
    # Первый поиск — в ветке диплинка (не нашли), второй — уже обычная регистрация
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(side_effect=[None, _Stop()]))
    state = _state()

    with pytest.raises(_Stop):
        await start_module.cmd_start(_start_message('/start tasks'), state, MagicMock(), db_user=None)

    start_deps.opened.assert_not_awaited()
    start_deps.campaign_lookup.assert_not_awaited()
    start_deps.referral_lookup.assert_not_awaited()
    assert 'referral_code' not in await state.get_data()


@pytest.mark.parametrize(('payload', 'treated_as_referral'), [('tasks', False), ('refAbC12345', True)])
@pytest.mark.asyncio
async def test_channel_check_does_not_turn_tasks_link_into_referral(monkeypatch, payload, treated_as_referral) -> None:
    """Диплинк, перехваченный проверкой подписки на канал, после «Я подписался».

    Второй параметр — контроль: обычный payload по-прежнему становится рефкодом.
    """
    state = _state()
    await state.set_data({'pending_start_payload': payload})
    campaign_lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(start_module, 'get_campaign_by_start_parameter', campaign_lookup)
    monkeypatch.setattr(start_module, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'delete_pending_payload_from_redis', AsyncMock())
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    monkeypatch.setattr(
        start_module,
        'channel_subscription_service',
        SimpleNamespace(
            bot=object(),
            invalidate_user_cache=AsyncMock(),
            is_user_subscribed_to_all=AsyncMock(return_value=True),
        ),
    )
    query = MagicMock()
    query.from_user = SimpleNamespace(id=TG_ID, username=None, first_name='Alice', last_name=None)
    query.answer = AsyncMock(side_effect=_Stop())  # дальше — регистрация, она тут не нужна

    await start_module.required_sub_channel_check(query, MagicMock(), state, MagicMock(), db_user=None)

    data = await state.get_data()
    assert 'pending_start_payload' not in data
    assert ('referral_code' in data) is treated_as_referral
    assert campaign_lookup.await_count == int(treated_as_referral)


def _fake_payload_redis(monkeypatch, payload: str) -> dict[int, str]:
    """Копия /start-payload в Redis, которую оставляет проверка подписки на канал."""
    redis = {TG_ID: payload}
    monkeypatch.setattr(start_module, 'get_pending_payload_from_redis', AsyncMock(side_effect=redis.get))
    monkeypatch.setattr(
        start_module, 'delete_pending_payload_from_redis', AsyncMock(side_effect=lambda tid: redis.pop(tid, None))
    )
    return redis


@pytest.mark.asyncio
async def test_start_tasks_held_by_channel_check_opens_tasks_once(start_deps, monkeypatch) -> None:
    """/start tasks придержала проверка подписки на канал, пользователь потом прислал
    обычный /start. Задания открываются один раз: следующий /start ведёт в главное
    меню, а не снова в задания (копия payload в Redis живёт час)."""
    redis = _fake_payload_redis(monkeypatch, 'tasks')
    main_menu = AsyncMock(side_effect=_Stop())  # первый шаг обычного /start зарегистрированного
    monkeypatch.setattr(start_module, 'find_phantom_user_by_username', main_menu)
    state = _state()
    user = SimpleNamespace(id=1, telegram_id=TG_ID, status='active', referred_by_id=None)
    db = MagicMock()

    await start_module.cmd_start(_start_message('/start'), state, db, db_user=user)

    start_deps.opened.assert_awaited_once()
    assert redis == {}
    assert not (await state.get_data()).get('pending_start_payload')

    with pytest.raises(_Stop):
        await start_module.cmd_start(_start_message('/start'), state, db, db_user=user)

    start_deps.opened.assert_awaited_once()
    main_menu.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_tasks_link_keeps_someone_elses_pending_payload(start_deps, monkeypatch) -> None:
    """Явная ссылка /start tasks не стирает чужой отложенный payload (триал, подарок…)."""
    redis = _fake_payload_redis(monkeypatch, 'trial')
    state = _state()
    user = SimpleNamespace(id=1, telegram_id=TG_ID, status='active', referred_by_id=None)

    await start_module.cmd_start(_start_message('/start tasks'), state, MagicMock(), db_user=user)

    start_deps.opened.assert_awaited_once()
    assert redis == {TG_ID: 'trial'}


@pytest.mark.parametrize('held_by_channel_check', [False, True])
@pytest.mark.asyncio
async def test_start_tasks_for_hidden_user_opens_the_normal_start(monkeypatch, held_by_channel_check) -> None:
    """Раздел скрыт (отключённая подписка, режим «только админы»): /start tasks — обычный
    старт с главным меню, ни списка заданий, ни «Раздел недоступен». Придержанный проверкой
    канала payload отработал и не уводит в задания на следующем /start."""
    redis = _fake_payload_redis(monkeypatch, 'tasks') if held_by_channel_check else None
    if redis is None:
        monkeypatch.setattr(start_module, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_campaign_by_start_parameter', AsyncMock(return_value=None))
    referral_lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(start_module, 'get_user_by_referral_code', referral_lookup)
    main_menu = AsyncMock(side_effect=_Stop())  # первый шаг обычного /start зарегистрированного
    monkeypatch.setattr(start_module, 'find_phantom_user_by_username', main_menu)
    access = AsyncMock(return_value=service.RewardTasksAccess(visible=False, hidden_reason='subscription_disabled'))
    monkeypatch.setattr(service, 'get_reward_tasks_access', access)
    state = _state()
    user = SimpleNamespace(id=1, telegram_id=TG_ID, status='active', referred_by_id=None)
    message = _start_message('/start' if held_by_channel_check else '/start tasks')

    with pytest.raises(_Stop):
        await start_module.cmd_start(message, state, MagicMock(), db_user=user)

    access.assert_awaited_once()
    main_menu.assert_awaited_once()
    message.answer.assert_not_awaited()
    referral_lookup.assert_not_awaited()
    data = await state.get_data()
    assert 'referral_code' not in data
    assert not data.get('pending_start_payload')
    if redis is not None:
        assert redis == {}


# ── Троттлинг ────────────────────────────────────────────────────────────────


def _throttled_message(**fields) -> MagicMock:
    message = MagicMock(spec=Message)
    message.from_user = SimpleNamespace(id=TG_ID)
    message.text = fields.get('text')
    message.photo = fields.get('photo')
    message.media_group_id = fields.get('media_group_id')
    message.caption = fields.get('caption')
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_throttling_lets_whole_album_through_while_collecting() -> None:
    middleware = ThrottlingMiddleware()
    state = _state()
    await state.set_state(RewardTaskStates.waiting_for_photos)
    handler = AsyncMock(return_value='ok')
    album = [_throttled_message(photo=_PHOTO, media_group_id='album') for _ in range(3)]
    album_document = _throttled_message(media_group_id='album')
    chatter = _throttled_message(text='ещё что-то')

    for message in [*album, album_document, chatter]:
        await middleware(handler, message, {'state': state})

    assert [call.args[0] for call in handler.await_args_list] == [*album, album_document]
    chatter.answer.assert_awaited_once()  # обычный троттлинг никуда не делся


@pytest.mark.asyncio
async def test_throttling_does_not_exempt_photos_captioned_with_a_command() -> None:
    """Фото с командой в подписи забирает хендлер команды (Command читает и caption),
    а не сбор скриншотов. Такие фото троттлятся как обычно — иначе пачка фото с
    «/contests» обходила бы лимит, и бот отвечал бы на каждое."""
    middleware = ThrottlingMiddleware()
    state = _state()
    await state.set_state(RewardTaskStates.waiting_for_photos)
    handler = AsyncMock(return_value='ok')
    first = _throttled_message(photo=_PHOTO, caption='мой ник @alice')
    commands = [
        _throttled_message(photo=_PHOTO, caption=caption, media_group_id='album')
        for caption in ('/contests', '  /contests', '/tasks@ledokol_bot')
    ]
    screenshot = _throttled_message(photo=_PHOTO, caption='ник в подписи', media_group_id='album')

    for message in [first, *commands, screenshot]:
        await middleware(handler, message, {'state': state})

    assert [call.args[0] for call in handler.await_args_list] == [first, screenshot]
    for message in commands:
        message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_throttling_outside_report_flow_is_unchanged() -> None:
    middleware = ThrottlingMiddleware()
    state = _state()
    await state.set_state(PromoCodeStates.waiting_for_code)
    handler = AsyncMock()
    first, second = (_throttled_message(photo=_PHOTO, media_group_id='album') for _ in range(2))

    await middleware(handler, first, {'state': state})
    await middleware(handler, second, {'state': state})

    assert [call.args[0] for call in handler.await_args_list] == [first]
    second.answer.assert_awaited_once()


# ── Тексты ───────────────────────────────────────────────────────────────────


def _ui_keys() -> dict[str, str | None]:
    """texts.t('REWARD_TASKS_…', default) из кода раздела и меню."""
    keys: dict[str, str | None] = {}
    for relative in ('app/handlers/reward_tasks.py', 'app/keyboards/reward_tasks.py', 'app/keyboards/inline.py'):
        tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 't'
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).startswith('REWARD_TASKS_')
            ):
                default = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else None
                keys[node.args[0].value] = default
    return keys


@pytest.mark.parametrize('language', LANGUAGES)
def test_ui_texts_exist_in_every_locale_with_same_placeholders(language) -> None:
    keys = _ui_keys()
    assert len(keys) > 30
    ru = json.loads((LOCALES_DIR / 'ru.json').read_text(encoding='utf-8'))
    locale = json.loads((LOCALES_DIR / f'{language}.json').read_text(encoding='utf-8'))
    for key, default in keys.items():
        assert key in locale, f'{language}: нет {key}'
        assert sorted(re.findall(r'\{\w+\}', locale[key])) == sorted(re.findall(r'\{\w+\}', ru[key])), key
        # Запасной текст в коде совпадает с русской локалью — нет двух версий одной фразы
        assert default == ru[key], key


# Число прямо перед существительным, которое с ним согласуется («1 скриншотов»,
# «1 screenshots»): такие тексты читаются правильно не для всех значений.
_NUMBER_BEFORE_COUNTED_NOUN = re.compile(r'\d+\s+(скриншот|screenshot|буст|boost|символ|charact)', re.IGNORECASE)


@pytest.mark.parametrize('language', LANGUAGES)
def test_counts_read_correctly_for_any_value(language) -> None:
    """max_photos — от 1 до 10, бустов и дней — сколько задаст админ: тексты не
    ставят существительное после числа («до 1 шт.», «Скриншотов — не больше 1»)."""
    texts = get_texts(language)
    for value in (1, 2, 3, 5, 10, 21):
        rendered = [
            texts.t('REWARD_TASKS_PHOTO_HINT').format(max=value),
            texts.t('REWARD_TASKS_PHOTO_PROMPT').format(max=value),
            texts.t('REWARD_TASKS_PHOTO_OVERFLOW').format(max=value),
            texts.t('REWARD_TASKS_PHOTO_STATUS').format(count=value, max=value),
            texts.t('REWARD_TASKS_BOOST_HINT').format(required=value),
            service.format_error_message('too_many_media', language, max=value),
            service.format_error_message('not_enough_boosts', language, found=value, required=value),
            service.format_error_message('comment_too_long', language, max=value),
        ]
        for text in rendered:
            assert not _NUMBER_BEFORE_COUNTED_NOUN.search(text), (language, value, text)


def test_english_never_says_one_screenshots_or_one_days() -> None:
    en = get_texts('en')
    assert service.format_reward_days(en, 1) == '+1 day'
    assert service.format_reward_days(en, 3) == '+3 days'
    assert en.t('REWARD_TASKS_PHOTO_HINT').format(max=1) == (
        'Complete the task and send screenshots as proof (up to 1) — an administrator will review your report.'
    )
    assert service.format_error_message('too_many_media', 'en', max=1) == 'Too many screenshots — the limit is 1.'


def test_boost_imperative_is_zabustite() -> None:
    """«Забустить» → «забустите» (основа на -ст-, как «пустите»), а не «забустьте»."""
    for language in LANGUAGES:
        locale = json.loads((LOCALES_DIR / f'{language}.json').read_text(encoding='utf-8'))
        assert not [key for key, value in locale.items() if 'Забустьте' in str(value)], language
    for relative in (
        'app/handlers/reward_tasks.py',
        'app/keyboards/reward_tasks.py',
        'app/services/reward_task_service.py',
    ):
        assert 'Забустьте' not in (ROOT / relative).read_text(encoding='utf-8'), relative
    assert get_texts('ru').t('REWARD_TASKS_BOOST_HINT').startswith('Забустите канал')
