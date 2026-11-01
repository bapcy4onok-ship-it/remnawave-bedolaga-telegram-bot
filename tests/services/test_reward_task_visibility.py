"""Кто видит раздел «Задания»: ``get_reward_tasks_access`` — одна точка для бота и кабинета.

Ответы владельца 2026-10-09:

- «скрыть функцию для отключенных подписок или забаненных людей»: любая DISABLED
  (пользователю она всегда «Отключена») и заблокированный/удалённый пользователь —
  раздела нет вовсе. Без подписки и с ограничением продления — раздел виден с
  объяснением.
- «пока оставь ее включенной для админов»: переключатель
  ``REWARD_TASKS_FOR_ALL_USERS`` (по умолчанию выключен) — пока он выключен,
  раздел видят только админы. Админ — тот, кто открывает админку: ADMIN_IDS,
  ADMIN_EMAILS (e-mail подтверждён доверенным способом) или активная роль
  кабинета уровня выше 0.

Переключатель меняется настоящим ``BotConfigurationService`` (через него пишут
админ-настройки и бота, и кабинета) и действует сразу, без перезапуска и без
кэша. Настоящая сессия — in-memory SQLite.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.crud.subscription import reset_subscription
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
from app.services import reward_task_service as service
from app.services.reward_task_service import RewardTasksAccess
from app.services.system_settings_service import BotConfigurationService, bot_configuration_service
from tests.fixtures.sqlite_memory import memory_session


TABLES = [
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

SWITCH = 'REWARD_TASKS_FOR_ALL_USERS'
USER_ID = 1
TG_ID = 555
ADMIN_TG_ID = 9001
ADMIN_EMAIL = 'boss@example.com'
SUBSCRIPTION_ID = 10


@pytest.fixture(autouse=True)
def _admins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'ADMIN_IDS', str(ADMIN_TG_ID))
    monkeypatch.setattr(settings, 'ADMIN_EMAILS', ADMIN_EMAIL)
    monkeypatch.setattr(settings, SWITCH, False)


# Состояние пользователя: подписка (статус, дней до конца) и поля пользователя.
STATES: dict[str, dict] = {
    'active': {'subscription': ('active', 20)},
    'trial': {'subscription': ('trial', 2), 'is_trial': True},
    'expired': {'subscription': ('expired', -3)},
    'disabled_time_left': {'subscription': ('disabled', 10)},
    'disabled_lapsed': {'subscription': ('disabled', -10)},
    'reset': {'subscription': ('active', 20), 'reset': True},  # настоящий «Обнулить»
    'no_subscription': {},
    'restricted': {'subscription': ('active', 20), 'restricted': True},
    'restricted_disabled': {'subscription': ('disabled', 10), 'restricted': True},
    'blocked': {'subscription': ('active', 20), 'status': 'blocked'},
    'deleted': {'subscription': ('active', 20), 'status': 'deleted'},
}

# Ожидание для того, кому раздел открыт (переключатель включён или админ): (visible, ineligible, hidden).
OPEN_EXPECTED: dict[str, tuple[bool, str | None, str | None]] = {
    'active': (True, None, None),
    'trial': (True, None, None),
    'expired': (True, None, None),
    'disabled_time_left': (False, None, 'subscription_disabled'),
    'disabled_lapsed': (False, None, 'subscription_disabled'),
    'reset': (False, None, 'subscription_disabled'),
    'no_subscription': (True, 'no_subscription', None),
    'restricted': (True, 'restricted', None),
    'restricted_disabled': (False, None, 'subscription_disabled'),
    'blocked': (False, None, 'user_blocked'),
    'deleted': (False, None, 'user_blocked'),
}

WHO = ('user', 'admin_ids', 'admin_email', 'admin_role')


@contextlib.asynccontextmanager
async def _world(monkeypatch: pytest.MonkeyPatch, state: str = 'active', who: str = 'user'):
    spec = STATES[state]
    async with memory_session(monkeypatch, TABLES) as db:
        now = datetime.now(UTC)
        db.add(
            User(
                id=USER_ID,
                telegram_id=ADMIN_TG_ID if who == 'admin_ids' else TG_ID,
                email=ADMIN_EMAIL if who == 'admin_email' else None,
                email_verified=who == 'admin_email',
                first_name='Alice',
                language='ru',
                status=spec.get('status', 'active'),
                restriction_subscription=spec.get('restricted', False),
            )
        )
        if 'subscription' in spec:
            status, days = spec['subscription']
            db.add(
                Subscription(
                    id=SUBSCRIPTION_ID,
                    user_id=USER_ID,
                    status=status,
                    is_trial=spec.get('is_trial', False),
                    end_date=now + timedelta(days=days),
                    traffic_limit_gb=0,
                    connected_squads=['squad-1'],
                    remnawave_short_id='s10',
                )
            )
        if who == 'admin_role':
            db.add(AdminRole(id=1, name='Marketer', level=30, permissions=['tasks:*']))
            db.add(UserRole(user_id=USER_ID, role_id=1, is_active=True))
        await db.commit()
        if spec.get('reset'):
            await reset_subscription(db, await db.get(Subscription, SUBSCRIPTION_ID))
        yield db


async def _access(db) -> tuple[bool, str | None, str | None]:
    access = await service.get_reward_tasks_access(db, await db.get(User, USER_ID))
    return access.visible, access.ineligible_reason, access.hidden_reason


# ── Матрица ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize('state', list(STATES))
@pytest.mark.parametrize('switch_on', [False, True], ids=['admins_only', 'for_all'])
@pytest.mark.parametrize('who', WHO)
@pytest.mark.asyncio
async def test_visibility_matrix(monkeypatch, who, switch_on, state):
    monkeypatch.setattr(settings, SWITCH, switch_on)
    async with _world(monkeypatch, state, who) as db:
        result = await _access(db)

    if switch_on or who != 'user' or state in ('blocked', 'deleted'):
        assert result == OPEN_EXPECTED[state]
    else:
        assert result == (False, None, 'admins_only')


def test_eligible_needs_a_visible_section_and_no_reason():
    assert RewardTasksAccess(visible=True).eligible is True
    assert RewardTasksAccess(visible=True, ineligible_reason='no_subscription').eligible is False
    assert RewardTasksAccess(visible=False, hidden_reason='admins_only').eligible is False


@pytest.mark.asyncio
async def test_no_user_is_hidden():
    access = await service.get_reward_tasks_access(None, None)
    assert (access.visible, access.hidden_reason) == (False, 'user_blocked')


@pytest.mark.asyncio
async def test_null_status_counts_as_active(monkeypatch):
    """Старые строки до дефолта колонки (status NULL) бот считает активными — как _lock_user."""
    monkeypatch.setattr(settings, SWITCH, True)
    async with _world(monkeypatch) as db:
        user = await db.get(User, USER_ID)
        user.status = None
        assert (await service.get_reward_tasks_access(db, user)).visible is True


# ── Кто админ ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ('role', 'assignment', 'admin'),
    [
        ({'level': 30}, {}, True),
        ({'level': 999}, {'expires_at_in_days': 3}, True),
        ({'level': 0}, {}, False),  # роль без уровня админку не открывает
        ({'level': 30, 'is_active': False}, {}, False),  # роль выключена
        ({'level': 30}, {'is_active': False}, False),  # назначение отозвано
        ({'level': 30}, {'expires_at_in_days': -1}, False),  # назначение истекло
    ],
    ids=['role', 'superadmin_until', 'level_0', 'role_off', 'revoked', 'expired'],
)
@pytest.mark.asyncio
async def test_cabinet_role_makes_an_admin_like_the_admin_panel(monkeypatch, role, assignment, admin):
    async with _world(monkeypatch) as db:
        db.add(AdminRole(id=7, name='Role', permissions=[], **role))
        expires = assignment.get('expires_at_in_days')
        db.add(
            UserRole(
                user_id=USER_ID,
                role_id=7,
                is_active=assignment.get('is_active', True),
                expires_at=None if expires is None else datetime.now(UTC) + timedelta(days=expires),
            )
        )
        await db.commit()
        user = await db.get(User, USER_ID)
        assert await service.is_reward_tasks_admin(db, user) is admin
        assert (await _access(db))[0] is admin


@pytest.mark.asyncio
async def test_email_admin_needs_a_trusted_verification(monkeypatch):
    """ADMIN_EMAILS — только с e-mail, подтверждённым доверенным способом (как вход в админку)."""
    async with _world(monkeypatch, who='admin_email') as db:
        user = await db.get(User, USER_ID)
        assert await service.is_reward_tasks_admin(db, user) is True
        user.email_verification_source = 'oauth_vk'
        assert await service.is_reward_tasks_admin(db, user) is False
        user.email_verification_source = 'cabinet'
        user.email_verified = False
        assert await service.is_reward_tasks_admin(db, user) is False


@pytest.mark.asyncio
async def test_support_moderator_is_not_an_admin(monkeypatch):
    """Модератор поддержки бота (SupportSettingsService) админку не открывает — раздела нет."""
    from app.services.support_settings_service import SupportSettingsService

    monkeypatch.setattr(SupportSettingsService, 'get_moderators', classmethod(lambda cls: [TG_ID]))
    async with _world(monkeypatch) as db:
        assert SupportSettingsService.is_moderator(TG_ID) is True
        assert await _access(db) == (False, None, 'admins_only')


@pytest.mark.asyncio
async def test_env_admin_needs_no_role_query(monkeypatch):
    """Админ из ADMIN_IDS определяется без SQL: таблиц ролей может и не быть."""
    async with memory_session(monkeypatch, [User.__table__, Subscription.__table__, Tariff.__table__]) as db:
        db.add(User(id=USER_ID, telegram_id=ADMIN_TG_ID, first_name='A', language='ru', status='active'))
        await db.commit()
        user = await db.get(User, USER_ID)
        assert await service.is_reward_tasks_admin(db, user) is True
        assert (await service.get_reward_tasks_access(db, user)).ineligible_reason == 'no_subscription'


# ── Переключатель в админ-настройках ─────────────────────────────────────────


@pytest.fixture
def overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Правки настроек не утекают в другие тесты (settings откатывает фикстура _admins)."""
    monkeypatch.setattr(BotConfigurationService, '_overrides_raw', {})


@pytest.mark.asyncio
async def test_switch_takes_effect_at_runtime_through_the_settings_service(monkeypatch, overrides):
    """set_value — то, чем пишут и бот («🔁 Переключить»), и кабинет (PUT /cabinet/admin/settings):
    значение меняется в памяти процесса и сразу действует, без перезапуска и кэша."""
    async with _world(monkeypatch) as db:
        assert await _access(db) == (False, None, 'admins_only')

        await bot_configuration_service.set_value(db, SWITCH, True)
        await db.commit()
        assert settings.REWARD_TASKS_FOR_ALL_USERS is True
        assert await _access(db) == (True, None, None)
        stored = await db.execute(select(SystemSetting.value).where(SystemSetting.key == SWITCH))
        assert stored.scalar_one() == 'true'  # переживёт перезапуск: initialize() читает system_settings

        await bot_configuration_service.set_value(db, SWITCH, False)
        await db.commit()
        assert await _access(db) == (False, None, 'admins_only')

        await bot_configuration_service.set_value(db, SWITCH, True)
        await bot_configuration_service.reset_value(db, SWITCH)  # «♻️ Сбросить» — к значению по умолчанию
        await db.commit()
        assert settings.REWARD_TASKS_FOR_ALL_USERS is False
        assert await _access(db) == (False, None, 'admins_only')


@pytest.mark.asyncio
async def test_has_active_tasks_cache_does_not_hide_the_switch(monkeypatch, overrides):
    """Кэш «есть ли задания» прогрет и живёт минуту, а переключатель действует с первого же вызова."""
    service.invalidate_active_tasks_cache()
    async with _world(monkeypatch) as db:
        now = datetime.now(UTC)
        db.add(RewardTask(id=1, kind='photo_report', title='Лайки', reward_days=1, created_at=now, updated_at=now))
        await db.commit()
        assert await service.has_active_tasks(db) is True  # кэш прогрет

        visible_before = (await service.get_reward_tasks_access(db, await db.get(User, USER_ID))).visible
        await bot_configuration_service.set_value(db, SWITCH, True)
        visible_after = (await service.get_reward_tasks_access(db, await db.get(User, USER_ID))).visible

        assert (visible_before, visible_after) == (False, True)
        assert await service.has_active_tasks(db) is True
    service.invalidate_active_tasks_cache()


def test_switch_definition_in_the_admin_settings():
    """Категория «🎁 Задания» с русским описанием; булев переключатель, по умолчанию выключен."""
    definition = bot_configuration_service.get_definition(SWITCH)
    assert (definition.category_key, definition.category_label) == ('REWARD_TASKS', '🎁 Задания')
    assert bot_configuration_service.is_toggle(SWITCH) is True
    assert bot_configuration_service.get_original_value(SWITCH) is False
    assert bot_configuration_service.is_read_only(SWITCH) is False
    assert bot_configuration_service.is_env_locked(SWITCH) is False
    assert bot_configuration_service.get_category_description('REWARD_TASKS').startswith('Задания за бесплатные дни')
    guidance = bot_configuration_service.get_setting_guidance(SWITCH)
    assert 'только админы' in guidance['description']
    assert 'без перезапуска' in guidance['warning']
    assert ('REWARD_TASKS', '🎁 Задания', 1) in bot_configuration_service.get_categories()
    assert [d.key for d in bot_configuration_service.get_settings_for_category('REWARD_TASKS')] == [SWITCH]
    assert bot_configuration_service.format_value_human(SWITCH, False) == '❌ ВЫКЛЮЧЕНО'


def test_switch_has_its_own_group_in_the_bot_settings(monkeypatch):
    """Бот: «⚙️ Админ-панель» → «⚙️ Настройки» → «🧩 Конфигурация бота» → «🎁 Задания»."""
    from app.handlers.admin import bot_configuration as ui

    grouped = {key: (title, items) for key, title, items in ui._get_grouped_categories()}
    title, items = grouped['reward_tasks']
    assert title == '🎁 Задания'
    assert items == [('REWARD_TASKS', '🎁 Задания', 1)]
    assert ui._get_group_status('reward_tasks') == ('🟡', 'Видят только админы')
    monkeypatch.setattr(settings, SWITCH, True)
    assert ui._get_group_status('reward_tasks') == ('🟢', 'Видят все пользователи')

    groups = [button.callback_data for row in ui._build_groups_keyboard().inline_keyboard for button in row]
    assert 'botcfg_group:reward_tasks:1' in groups
    keyboard = ui._build_settings_keyboard('REWARD_TASKS', 'reward_tasks', 1, 'ru')
    token = bot_configuration_service.get_callback_token(SWITCH)
    [setting_button] = [
        button for row in keyboard.inline_keyboard for button in row if button.callback_data.endswith(token)
    ]
    assert 'Reward Tasks For All Users' in setting_button.text
    toggle = ui._build_setting_keyboard(SWITCH, 'reward_tasks', 1, 1)
    assert any(
        button.text == '🔁 Переключить' and button.callback_data == f'botcfg_toggle:reward_tasks:1:1:{token}'
        for row in toggle.inline_keyboard
        for button in row
    )
