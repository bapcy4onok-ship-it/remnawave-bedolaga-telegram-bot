"""Юнит-тесты правил «Заданий»: выбор подписки, статусы, проверка отчётов и бустов,
тексты ошибок, уведомления и перенос отчётов при слиянии аккаунтов."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services import reward_task_service as service
from app.services.reward_task_service import (
    RewardTaskError,
    RewardTaskErrorCode,
    SubscriptionCandidate,
    TaskUserStatus,
)
from app.utils.photo_sizes import pick_thumbnail_photo_size


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
FILE_A = 'AgACAgIAAxkBAAIBQ2ZxAAAAAAAAAAAAAAAAAAAA'
FILE_B = 'AgACAgIAAxkBAAIBQ2ZxBBBBBBBBBBBBBBBBBBBB'
LOCALES_DIR = Path(__file__).resolve().parents[2] / 'app' / 'localization' / 'locales'


@pytest.fixture(autouse=True)
def _tasks_for_all_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """Раздел открыт всем: здесь проверяются правила заданий, а не режим «только админы»."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', True)


def _candidate(id_: int, status: str, days: float, *, daily: bool = False) -> SubscriptionCandidate:
    return SubscriptionCandidate(id=id_, status=status, end_date=NOW + timedelta(days=days), is_daily=daily)


def _task(**overrides) -> SimpleNamespace:
    base = {
        'id': 7,
        'kind': 'photo_report',
        'title': 'Задание',
        'is_active': True,
        'max_photos': 3,
        'min_boosts': 1,
        'reward_days': 2,
        'channel_id': '-1003822976183',
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(**overrides) -> SimpleNamespace:
    base = {'id': 1, 'telegram_id': 555, 'language': 'ru', 'restriction_subscription': False}
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Выбор подписки ───────────────────────────────────────────────────────────


def _choose(*candidates: SubscriptionCandidate) -> SubscriptionCandidate | None:
    return service.choose_reward_subscription(candidates)


def _reason(*candidates: SubscriptionCandidate) -> str:
    return service.no_reward_subscription_reason(candidates)


def test_alive_subscription_with_latest_end_wins():
    chosen = _choose(_candidate(1, 'active', 5), _candidate(2, 'trial', 9), _candidate(3, 'expired', 100))
    assert chosen.id == 2


def test_limited_counts_as_alive():
    assert _choose(_candidate(1, 'limited', 1), _candidate(2, 'disabled', 50)).id == 1
    # Живая важнее истёкшей, даже если та кончилась позже.
    assert _choose(_candidate(1, 'limited', -1), _candidate(2, 'expired', -0.5)).id == 1


def test_non_daily_preferred_over_daily():
    chosen = _choose(_candidate(1, 'active', 30, daily=True), _candidate(2, 'active', 3))
    assert chosen.id == 2


def test_daily_used_when_it_is_the_only_alive_one():
    chosen = _choose(_candidate(1, 'active', 1, daily=True), _candidate(2, 'expired', 0))
    assert chosen.id == 1


def test_most_recently_ended_expired_subscription_is_revived():
    # Из истёкших оживает та, что кончилась последней (решение владельца 2026-10-08).
    # Отключённая, хоть и кончилась позже всех, не в счёт (ответ владельца 2026-10-09).
    chosen = _choose(_candidate(1, 'expired', -30), _candidate(2, 'disabled', -2), _candidate(3, 'expired', -10))
    assert chosen.id == 3


@pytest.mark.parametrize(
    'days', [20, 1e-6, 0, -1e-6, -5, -400], ids=['left', 'just_left', 'now', 'just_ended', 'ended', 'long_ago']
)
def test_disabled_subscription_is_never_chosen_whatever_its_end_date(days):
    """Ответ владельца 2026-10-09: DISABLED не оживляется никогда — ни с остатком срока, ни с вышедшим.

    До него «Обнулить», «Принудительная очистка», синк для отсутствующих в панели и
    триал, выключенный за отписку от канала (DISABLED с вышедшим сроком), оживали наградой.
    """
    candidates = [_candidate(1, 'disabled', days), _candidate(2, 'pending', 30)]
    assert _choose(*candidates) is None
    assert _reason(*candidates) == 'subscription_disabled'


def test_expired_row_is_chosen_whatever_its_end_date():
    # EXPIRED — истёкшая, сколько бы ни показывала дата (админ мог поставить её в будущее).
    assert _choose(_candidate(1, 'expired', 3)).id == 1
    assert _choose(_candidate(1, 'expired', -400)).id == 1


def test_no_subscription_reason():
    assert _reason() == 'no_subscription'
    assert _reason(_candidate(1, 'pending', 3)) == 'no_subscription'


def test_any_disabled_row_means_subscription_disabled():
    assert _reason(_candidate(1, 'disabled', -1)) == 'subscription_disabled'
    assert _reason(_candidate(1, 'pending', 3), _candidate(2, 'disabled', 30)) == 'subscription_disabled'
    assert _reason(SubscriptionCandidate(id=1, status='disabled', end_date=None)) == 'subscription_disabled'


def test_expired_sibling_is_revived_next_to_disabled_one():
    # Мультитариф: соседняя отключённая не мешает оживить истёкшую.
    assert _choose(_candidate(1, 'disabled', 25), _candidate(2, 'expired', -3)).id == 2


def test_older_expired_sibling_wins_over_newer_disabled_one():
    """До 2026-10-09 из закончившихся оживала та, что кончилась последней, — и DISABLED тоже."""
    assert _choose(_candidate(1, 'disabled', -1), _candidate(2, 'expired', -10)).id == 2
    assert _choose(_candidate(1, 'disabled', -10), _candidate(2, 'expired', -1)).id == 2


def test_pending_draft_and_nothing_mean_no_subscription():
    assert _choose(_candidate(1, 'pending', 10)) is None
    assert _choose(_candidate(1, 'pending', -10)) is None
    assert _choose() is None


def test_naive_end_dates_are_compared_as_utc():
    naive = SubscriptionCandidate(id=1, status='active', end_date=datetime(2030, 1, 1))
    aware = SubscriptionCandidate(id=2, status='active', end_date=datetime(2029, 1, 1, tzinfo=UTC))
    assert _choose(naive, aware).id == 1
    # Истёкшие сравниваются так же.
    naive = SubscriptionCandidate(id=1, status='expired', end_date=datetime(2026, 1, 1))
    aware = SubscriptionCandidate(id=2, status='expired', end_date=datetime(2025, 1, 1, tzinfo=UTC))
    assert _choose(naive, aware).id == 1


# ── Статус задания для пользователя ──────────────────────────────────────────


def _submission(status: str, *, reason: str | None = None, reviewed_at=None) -> SimpleNamespace:
    return SimpleNamespace(status=status, reject_reason=reason, reviewed_at=reviewed_at, created_at=NOW)


def test_task_statuses():
    task = _task()
    assert service.resolve_task_view(task, []).status == TaskUserStatus.AVAILABLE

    view = service.resolve_task_view(task, [_submission('rejected', reason='нет лайков')])
    assert (view.status, view.reject_reason) == ('rejected', 'нет лайков')

    # Последний отчёт на проверке — «на проверке», хотя раньше был отклонён.
    pending = service.resolve_task_view(task, [_submission('pending'), _submission('rejected', reason='x')])
    assert (pending.status, pending.reject_reason) == ('pending', None)

    done = service.resolve_task_view(task, [_submission('rejected'), _submission('approved', reviewed_at=NOW)])
    assert (done.status, done.completed_at) == ('completed', NOW)


def test_statuses_are_plain_strings_for_serialization():
    view = service.resolve_task_view(_task(), [])
    assert type(view.status) is str


# ── Скриншоты ────────────────────────────────────────────────────────────────


def _size(file_id: str, width: int) -> SimpleNamespace:
    return SimpleNamespace(file_id=file_id, file_unique_id=f'u-{file_id}', width=width, height=width)


def test_thumbnail_is_smallest_size_at_least_320px():
    sizes = [_size('s90', 90), _size('s320', 320), _size('s800', 800), _size('s1280', 1280)]
    assert pick_thumbnail_photo_size(sizes).file_id == 's320'
    assert pick_thumbnail_photo_size([_size('a', 90), _size('b', 400), _size('c', 330)]).file_id == 'c'


def test_thumbnail_falls_back_to_largest_for_small_photos():
    assert pick_thumbnail_photo_size([_size('s90', 90), _size('s200', 200)]).file_id == 's200'
    assert pick_thumbnail_photo_size([]) is None


def test_build_photo_media_item_from_message_photo():
    item = service.build_photo_media_item([_size('s90', 90), _size('s320', 320), _size('s1280', 1280)])
    assert item == {'type': 'photo', 'file_id': 's1280', 'file_unique_id': 'u-s1280', 'thumb_file_id': 's320'}
    assert service.build_photo_media_item([]) is None


def test_media_items_are_normalized_and_deduplicated():
    items = service.normalize_media_items(
        [
            {'file_id': FILE_A, 'file_unique_id': 'AQADuniqueA', 'thumb_file_id': FILE_B},
            SimpleNamespace(file_id=FILE_B, file_unique_id=None, thumb_file_id=None),
            {'file_id': FILE_A, 'file_unique_id': 'AQADuniqueA'},  # тот же файл ещё раз
        ],
        max_items=3,
    )
    assert items == [
        {'type': 'photo', 'file_id': FILE_A, 'file_unique_id': 'AQADuniqueA', 'thumb_file_id': FILE_B},
        {'type': 'photo', 'file_id': FILE_B, 'file_unique_id': None, 'thumb_file_id': None},
    ]


@pytest.mark.parametrize(
    ('items', 'code', 'params'),
    [
        ([], 'media_required', {}),
        (None, 'media_required', {}),
        ([{'file_id': 'short'}], 'invalid_media', {}),
        ([{'file_id': FILE_A + '<script>'}], 'invalid_media', {}),
        ([{'file_id': FILE_A, 'thumb_file_id': '../etc/passwd'}], 'invalid_media', {}),
        ([{'file_id': FILE_A}, {'file_id': FILE_B}], 'too_many_media', {'max': 1}),
    ],
)
def test_media_items_validation(items, code, params):
    with pytest.raises(RewardTaskError) as error:
        service.normalize_media_items(items, max_items=1)
    assert error.value.code == code
    assert error.value.params == params


# ── Бусты ────────────────────────────────────────────────────────────────────


def test_only_unexpired_boosts_are_counted():
    boosts = [
        SimpleNamespace(expiration_date=NOW + timedelta(days=3)),
        SimpleNamespace(expiration_date=NOW - timedelta(seconds=1)),
        SimpleNamespace(expiration_date=(NOW + timedelta(hours=1)).replace(tzinfo=None)),
        SimpleNamespace(expiration_date=int((NOW + timedelta(days=1)).timestamp())),
        SimpleNamespace(expiration_date=None),
    ]
    assert service.count_active_boosts(boosts, now=NOW) == 3
    assert service.count_active_boosts([], now=NOW) == 0


@pytest.fixture
def boost_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """check_boost до начисления: БД-проверки и начисление подменены."""
    env = SimpleNamespace(
        has_submission=AsyncMock(return_value=False),
        ineligible=AsyncMock(return_value=None),
        rate_limited=AsyncMock(return_value=False),
        grant=AsyncMock(return_value=(SimpleNamespace(reward_days=3), None)),
    )
    monkeypatch.setattr(service, '_has_submission', env.has_submission)
    monkeypatch.setattr(service, 'get_ineligible_reason', env.ineligible)
    monkeypatch.setattr(service.RateLimitCache, 'is_rate_limited', env.rate_limited)
    monkeypatch.setattr(service, '_grant', env.grant)
    return env


def _bot(**kwargs) -> MagicMock:
    bot = MagicMock()
    bot.get_user_chat_boosts = AsyncMock(**kwargs)
    return bot


@pytest.mark.asyncio
async def test_boost_requires_telegram_account(boost_env):
    bot = _bot()
    with pytest.raises(RewardTaskError) as error:
        await service.check_boost(AsyncMock(), _user(telegram_id=None), _task(kind='channel_boost'), bot)
    assert error.value.code == 'telegram_required'
    bot.get_user_chat_boosts.assert_not_called()


@pytest.mark.asyncio
async def test_boost_wrong_kind_or_inactive_task(boost_env):
    with pytest.raises(RewardTaskError) as error:
        await service.check_boost(AsyncMock(), _user(), _task(kind='photo_report'), _bot())
    assert error.value.code == 'wrong_task_kind'
    with pytest.raises(RewardTaskError) as error:
        await service.check_boost(AsyncMock(), _user(), _task(kind='channel_boost', is_active=False), _bot())
    assert error.value.code == 'task_inactive'


@pytest.mark.asyncio
async def test_boost_rate_limit_is_checked_before_telegram(boost_env):
    boost_env.rate_limited.return_value = True
    bot = _bot()
    with pytest.raises(RewardTaskError) as error:
        await service.check_boost(AsyncMock(), _user(), _task(kind='channel_boost'), bot)
    assert error.value.code == 'rate_limited'
    assert boost_env.rate_limited.await_args.args[:2] == (1, 'reward_task_boost')
    assert boost_env.rate_limited.await_args.kwargs == {'limit': 5, 'window': 60}
    bot.get_user_chat_boosts.assert_not_called()


@pytest.mark.asyncio
async def test_boost_ineligible_user_never_reaches_telegram(boost_env):
    boost_env.ineligible.return_value = 'no_subscription'
    bot = _bot()
    with pytest.raises(RewardTaskError) as error:
        await service.check_boost(AsyncMock(), _user(), _task(kind='channel_boost'), bot)
    assert error.value.code == 'no_subscription'
    bot.get_user_chat_boosts.assert_not_called()
    boost_env.rate_limited.assert_not_called()


@pytest.mark.asyncio
async def test_boost_of_user_with_disabled_subscription_is_tasks_unavailable(boost_env):
    """Раздел скрыт (ответ владельца 2026-10-09): ни Telegram, ни лимита, ни начисления."""
    boost_env.ineligible.return_value = 'subscription_disabled'
    bot = _bot()
    with pytest.raises(RewardTaskError) as error:
        await service.check_boost(AsyncMock(), _user(), _task(kind='channel_boost'), bot)
    assert error.value.code == 'tasks_unavailable'
    bot.get_user_chat_boosts.assert_not_called()
    boost_env.rate_limited.assert_not_called()
    boost_env.grant.assert_not_called()


@pytest.mark.asyncio
async def test_boost_in_admins_only_mode(boost_env, monkeypatch):
    """Переключатель выключен: не-админу раздела нет, админ проверяет буст как обычно."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', False)
    is_admin = AsyncMock(return_value=False)
    monkeypatch.setattr(service, 'is_reward_tasks_admin', is_admin)
    bot = _bot(return_value=SimpleNamespace(boosts=[SimpleNamespace(expiration_date=NOW + timedelta(days=900))]))

    with pytest.raises(RewardTaskError) as error:
        await service.check_boost(AsyncMock(), _user(), _task(kind='channel_boost'), bot)
    assert error.value.code == 'tasks_unavailable'
    bot.get_user_chat_boosts.assert_not_called()

    is_admin.return_value = True
    result = await service.check_boost(AsyncMock(), _user(), _task(kind='channel_boost'), bot)
    assert result.reward_days == 3
    bot.get_user_chat_boosts.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_error_becomes_generic_boost_check_failed(boost_env):
    from aiogram.exceptions import TelegramBadRequest

    error_from_api = TelegramBadRequest(method=MagicMock(), message='Bad Request: CHAT_ADMIN_REQUIRED')
    with pytest.raises(RewardTaskError) as error:
        await service.check_boost(AsyncMock(), _user(), _task(kind='channel_boost'), _bot(side_effect=error_from_api))
    assert error.value.code == 'boost_check_failed'
    assert error.value.params == {}
    boost_env.grant.assert_not_called()


@pytest.mark.asyncio
async def test_username_channel_is_passed_as_is(boost_env):
    bot = _bot(return_value=SimpleNamespace(boosts=[SimpleNamespace(expiration_date=NOW + timedelta(days=900))]))
    await service.check_boost(AsyncMock(), _user(), _task(kind='channel_boost', channel_id='@ledokol_vpn'), bot)
    bot.get_user_chat_boosts.assert_awaited_once_with(chat_id='@ledokol_vpn', user_id=555)


@pytest.mark.asyncio
async def test_successful_boost_creates_auto_submission(boost_env):
    future = datetime.now(UTC) + timedelta(days=30)
    bot = _bot(return_value=SimpleNamespace(boosts=[SimpleNamespace(expiration_date=future)] * 2))
    user, task = _user(), _task(kind='channel_boost', min_boosts=2)

    result = await service.check_boost(AsyncMock(), user, task, bot)

    assert result.reward_days == 3
    submission = boost_env.grant.await_args.args[1]
    assert (submission.status, submission.source, submission.boost_count, submission.media_items) == (
        'pending',
        'auto',
        2,
        [],
    )
    assert boost_env.grant.await_args.kwargs['reviewer_id'] is None
    assert boost_env.grant.await_args.kwargs['restore'] == (user, task)


# ── Фотоотчёт: проверки до записи ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_rejects_completed_and_pending(monkeypatch):
    has_submission = AsyncMock(side_effect=lambda db, task_id, user_id, status: status == 'approved')
    monkeypatch.setattr(service, '_has_submission', has_submission)
    with pytest.raises(RewardTaskError) as error:
        await service.submit_photo_report(AsyncMock(), _user(), _task(), [{'file_id': FILE_A}])
    assert error.value.code == 'already_completed'

    has_submission.side_effect = lambda db, task_id, user_id, status: status == 'pending'
    with pytest.raises(RewardTaskError) as error:
        await service.submit_photo_report(AsyncMock(), _user(), _task(), [{'file_id': FILE_A}])
    assert error.value.code == 'already_pending'


@pytest.mark.asyncio
async def test_submit_validates_comment_and_source(monkeypatch):
    monkeypatch.setattr(service, '_has_submission', AsyncMock(return_value=False))
    monkeypatch.setattr(service, 'get_ineligible_reason', AsyncMock(return_value=None))
    with pytest.raises(RewardTaskError) as error:
        await service.submit_photo_report(AsyncMock(), _user(), _task(), [{'file_id': FILE_A}], 'x' * 501)
    assert error.value.code == 'comment_too_long'
    assert error.value.params == {'max': 500}

    with pytest.raises(ValueError, match='source'):
        await service.submit_photo_report(AsyncMock(), _user(), _task(), [{'file_id': FILE_A}], source='auto')


@pytest.mark.asyncio
@pytest.mark.parametrize('switch_on', [True, False])
async def test_submit_of_hidden_user_is_tasks_unavailable(monkeypatch, switch_on):
    """Отключённая подписка или режим «только админы» — отчёт не принимается: раздела нет."""
    monkeypatch.setattr(settings, 'REWARD_TASKS_FOR_ALL_USERS', switch_on)
    monkeypatch.setattr(service, 'is_reward_tasks_admin', AsyncMock(return_value=False))
    monkeypatch.setattr(service, '_has_submission', AsyncMock(return_value=False))
    reason = 'subscription_disabled' if switch_on else None
    monkeypatch.setattr(service, 'get_ineligible_reason', AsyncMock(return_value=reason))
    db = AsyncMock()
    with pytest.raises(RewardTaskError) as error:
        await service.submit_photo_report(db, _user(), _task(), [{'file_id': FILE_A}])
    assert error.value.code == 'tasks_unavailable'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_to_boost_task_is_refused():
    with pytest.raises(RewardTaskError) as error:
        await service.submit_photo_report(AsyncMock(), _user(), _task(kind='channel_boost'), [{'file_id': FILE_A}])
    assert error.value.code == 'wrong_task_kind'


# ── Тексты ошибок и локали ───────────────────────────────────────────────────


def test_error_message_is_localized_with_params():
    error = RewardTaskError('not_enough_boosts', found=0, required=1)
    assert service.format_error_message(error, 'ru') == ('Бустов найдено: 0 из 1. Забустите канал и проверьте ещё раз.')
    assert service.format_error_message(error, 'en') == 'Boosts found: 0 of 1. Boost the channel and check again.'
    assert service.format_error_message('too_many_media', 'en', max=3) == 'Too many screenshots — the limit is 3.'


def test_unknown_code_and_missing_params_do_not_crash():
    assert service.format_error_message('something_new', 'en') == 'Something went wrong. Please try again later.'
    # Нет параметров — плейсхолдер пустой, но не KeyError.
    assert 'из' in service.format_error_message('not_enough_boosts', 'ru')


def test_no_subscription_text_is_the_owner_wording():
    assert service.format_error_message('no_subscription', 'ru') == (
        'Сначала активируйте пробный период или оформите подписку, затем возвращайтесь к заданиям.'
    )


@pytest.mark.parametrize(
    ('language', 'text'),
    [
        ('ru', 'Раздел недоступен'),
        ('en', 'This section is unavailable'),
        ('ua', 'Розділ недоступний'),
        ('fa', 'این بخش در دسترس نیست'),
        ('zh', '此栏目不可用'),
    ],
)
def test_tasks_unavailable_is_a_short_neutral_text(language, text):
    assert service.format_error_message('tasks_unavailable', language) == text


def test_every_user_facing_code_has_a_message_key():
    admin_only = {'not_found', 'not_pending', 'invalid_reason', 'invalid_task', 'task_has_submissions'}
    for code in RewardTaskErrorCode:
        if code.value in admin_only:
            continue
        assert code.value in service._ERROR_MESSAGE_KEYS, code


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'fa', 'zh'])
def test_reward_task_keys_exist_in_every_locale_with_same_placeholders(language):
    keys = set(service._ERROR_MESSAGE_KEYS.values()) | {
        service.NOTIFY_APPROVED_KEY,
        service.NOTIFY_REJECTED_KEY,
        service.NOTIFY_APPROVED_BUTTON_KEY,
        service.NOTIFY_REJECTED_BUTTON_KEY,
        'REWARD_TASKS_REWARD_DAY',
        'REWARD_TASKS_REWARD_DAYS',
    }
    ru = json.loads((LOCALES_DIR / 'ru.json').read_text(encoding='utf-8'))
    locale = json.loads((LOCALES_DIR / f'{language}.json').read_text(encoding='utf-8'))
    for key in keys:
        assert key in locale, f'{language}: {key}'
        assert sorted(re.findall(r'\{\w+\}', locale[key])) == sorted(re.findall(r'\{\w+\}', ru[key])), key


# ── Поля задания ─────────────────────────────────────────────────────────────


def test_boost_task_requires_and_normalizes_channel():
    fields = service.validate_task_fields(
        {'kind': 'channel_boost', 'title': ' Буст ', 'reward_days': 3, 'channel_id': '3822976183'}
    )
    assert fields['channel_id'] == '-1003822976183'
    assert fields['title'] == 'Буст'
    assert (fields['min_boosts'], fields['max_photos'], fields['is_active'], fields['sort_order']) == (1, 3, True, 0)

    username = service.validate_task_fields(
        {'kind': 'channel_boost', 'title': 'x', 'reward_days': 3, 'channel_id': '@ledokol_vpn'}
    )
    assert username['channel_id'] == '@ledokol_vpn'

    for bad in (None, '', '12345', '@ab', 'channel'):
        with pytest.raises(RewardTaskError) as error:
            service.validate_task_fields({'kind': 'channel_boost', 'title': 'x', 'reward_days': 3, 'channel_id': bad})
        assert error.value.params['field'] == 'channel_id'


def test_photo_task_drops_channel_and_checks_ranges():
    fields = service.validate_task_fields(
        {'kind': 'photo_report', 'title': 'Фото', 'reward_days': 1, 'channel_id': '-1003822976183', 'max_photos': 10}
    )
    assert fields['channel_id'] is None
    assert fields['max_photos'] == 10

    for field, value in (
        ('reward_days', 0),
        ('reward_days', 366),
        ('reward_days', True),
        ('max_photos', 11),
        ('min_boosts', 0),
        ('min_boosts', 101),
        ('sort_order', 10**7),
        ('title', '   '),
        ('title', 'x' * 201),
        ('description', 'x' * 3001),
        ('kind', 'quiz'),
        ('is_active', 'yes'),
    ):
        data = {'kind': 'photo_report', 'title': 'Фото', 'reward_days': 1, field: value}
        with pytest.raises(RewardTaskError) as error:
            service.validate_task_fields(data)
        assert error.value.code == 'invalid_task'
        assert error.value.params['field'] == field


@pytest.mark.parametrize(
    'url',
    ['javascript:alert(1)', 'data:text/html,x', 'tg://resolve?domain=x', 'https://', 'https://t.me/a b', 'ftp://x.y'],
)
def test_action_url_must_be_http_link(url):
    with pytest.raises(RewardTaskError) as error:
        service.validate_task_fields({'kind': 'photo_report', 'title': 'x', 'reward_days': 1, 'action_url': url})
    assert error.value.params['field'] == 'action_url'


def test_action_url_accepts_boost_link_and_blank():
    fields = service.validate_task_fields(
        {'kind': 'photo_report', 'title': 'x', 'reward_days': 1, 'action_url': ' https://t.me/boost/LEDOKOL '}
    )
    assert fields['action_url'] == 'https://t.me/boost/LEDOKOL'
    blank = service.validate_task_fields({'kind': 'photo_report', 'title': 'x', 'reward_days': 1, 'action_url': ''})
    assert blank['action_url'] is None


# ── Уведомления ──────────────────────────────────────────────────────────────


def test_notice_is_skipped_for_web_only_and_blocked_users():
    values = {'title': 'T', 'reward_days': 3}
    assert service._build_notice(_user(telegram_id=None), service.NOTIFY_APPROVED_KEY, **values) is None
    blocked = SimpleNamespace(telegram_id=1, language='ru', status='blocked')
    assert service._build_notice(blocked, service.NOTIFY_APPROVED_KEY, **values) is None
    notice = service._build_notice(
        SimpleNamespace(telegram_id=1, language='en', status='active'), service.NOTIFY_APPROVED_KEY, **values
    )
    assert notice.text == '✅ Task «T» completed: +3 days added to your subscription'
    assert notice.reply_markup is None  # кнопку добавляет только явный button


@pytest.mark.asyncio
async def test_notifications_are_best_effort_and_close_own_bot(monkeypatch):
    async def send_message(**kwargs):
        if kwargs['chat_id'] == 1:
            raise RuntimeError('blocked')

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=send_message)
    bot.session.close = AsyncMock()
    monkeypatch.setattr(service, 'create_bot', lambda: bot)

    await service._deliver_notices([service._Notice(1, 'a'), None, service._Notice(2, 'b')])

    # Отправка параллельная — порядок вызовов не важен; без кнопки reply_markup не передаётся.
    sent = sorted((call.kwargs for call in bot.send_message.await_args_list), key=lambda kwargs: kwargs['chat_id'])
    assert sent == [
        {'chat_id': 1, 'text': 'a', 'parse_mode': 'HTML'},
        {'chat_id': 2, 'text': 'b', 'parse_mode': 'HTML'},
    ]
    bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_notices_are_sent_concurrently_with_a_bound_and_failures_are_isolated():
    in_flight = 0
    peak = 0
    delivered: list[int] = []

    async def send_message(**kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)
            if kwargs['chat_id'] == 3:
                raise RuntimeError('Forbidden: bot was blocked by the user')
            delivered.append(kwargs['chat_id'])
        finally:
            in_flight -= 1

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=send_message)

    await service._deliver_notices([service._Notice(chat_id, f'text {chat_id}') for chat_id in range(1, 13)], bot=bot)

    assert peak == service.NOTIFY_CONCURRENCY == 5
    assert bot.send_message.await_count == 12
    assert sorted(delivered) == [chat_id for chat_id in range(1, 13) if chat_id != 3]


@pytest.mark.asyncio
async def test_hanging_notice_times_out_without_holding_back_others(monkeypatch):
    monkeypatch.setattr(service, 'NOTIFY_TIMEOUT_SECONDS', 0.05)
    delivered: list[int] = []

    async def send_message(**kwargs):
        if kwargs['chat_id'] == 1:
            await asyncio.Event().wait()  # Telegram не отвечает
        delivered.append(kwargs['chat_id'])

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=send_message)

    await service._deliver_notices([service._Notice(chat_id, 't') for chat_id in (1, 2, 3)], bot=bot)

    assert sorted(delivered) == [2, 3]


@pytest.mark.parametrize(
    ('language', 'days', 'expected'),
    [
        ('en', 1, '✅ Task «T» completed: +1 day added to your subscription'),
        ('en', 3, '✅ Task «T» completed: +3 days added to your subscription'),
        ('ru', 1, '✅ Задание «T» выполнено: +1 дн. к подписке'),
        ('ua', 2, '✅ Завдання «T» виконано: +2 дн. до підписки'),
    ],
)
def test_approved_notice_uses_the_bot_reward_forms(language, days, expected):
    user = SimpleNamespace(telegram_id=1, language=language, status='active')
    notice = service._build_notice(user, service.NOTIFY_APPROVED_KEY, reward_days=days, title='T')
    assert notice.text == expected


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'fa', 'zh'])
def test_notices_render_with_buttons_in_every_locale(language):
    from app.localization.texts import get_texts

    user = SimpleNamespace(telegram_id=1, language=language, status='active')
    approved = service._build_notice(
        user,
        service.NOTIFY_APPROVED_KEY,
        button=(service.NOTIFY_APPROVED_BUTTON_KEY, service.REWARD_TASKS_MENU_CALLBACK),
        reward_days=1,
        title='T',
    )
    rejected = service._build_notice(
        user,
        service.NOTIFY_REJECTED_KEY,
        button=(service.NOTIFY_REJECTED_BUTTON_KEY, f'{service.TASK_CALLBACK_PREFIX}7'),
        title='T',
        reason='Нет лайков.',
    )

    assert service.format_reward_days(get_texts(language), 1) in approved.text
    assert 'Нет лайков.\n' in rejected.text  # причина — отдельной строкой, без «..»
    assert '..' not in rejected.text
    for notice, callback_data in ((approved, 'reward_tasks_menu'), (rejected, 'reward_task:7')):
        assert '{' not in notice.text
        [[button]] = notice.reply_markup.inline_keyboard
        assert button.text
        assert button.callback_data == callback_data


def test_notice_button_text_drops_tg_emoji_markup(monkeypatch):
    """Текст кнопки Telegram не парсит как HTML: <tg-emoji> из переопределённых локалей срезается."""
    values = {'KEY': 'Текст', 'BUTTON': '<tg-emoji emoji-id="5368324170671202286">🎁</tg-emoji> Задания'}
    monkeypatch.setattr(
        service, 'get_texts', lambda language=None: SimpleNamespace(get=lambda key, default=None: values.get(key))
    )
    user = SimpleNamespace(telegram_id=1, language='ru', status='active')

    notice = service._build_notice(user, 'KEY', button=('BUTTON', 'reward_tasks_menu'))

    [[button]] = notice.reply_markup.inline_keyboard
    assert button.text == '🎁 Задания'


def test_bot_screens_and_notifications_share_formatter_and_callbacks():
    from app.keyboards import reward_tasks as keyboards

    assert keyboards.format_reward_days is service.format_reward_days
    assert keyboards.TASK_CALLBACK_PREFIX == service.TASK_CALLBACK_PREFIX == 'reward_task:'
    assert keyboards.REWARD_TASKS_MENU_CALLBACK == service.REWARD_TASKS_MENU_CALLBACK == 'reward_tasks_menu'


@pytest.mark.asyncio
async def test_passed_bot_is_not_closed_and_nothing_to_send_creates_nothing(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(service, 'create_bot', factory)
    await service._deliver_notices([None])
    factory.assert_not_called()

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.session.close = AsyncMock()
    await service._deliver_notices([service._Notice(1, 'a')], bot=bot)
    bot.session.close.assert_not_called()
    factory.assert_not_called()


# ── Кнопка в меню ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_menu_visibility_is_cached_and_invalidated(monkeypatch):
    service.invalidate_active_tasks_cache()
    count = AsyncMock(return_value=2)
    monkeypatch.setattr(service, 'count_active_tasks', count)

    assert await service.has_active_tasks(AsyncMock()) is True
    assert await service.has_active_tasks(AsyncMock()) is True
    assert count.await_count == 1

    service.invalidate_active_tasks_cache()
    count.return_value = 0
    assert await service.has_active_tasks(AsyncMock()) is False
    assert count.await_count == 2

    service.invalidate_active_tasks_cache()
    count.side_effect = RuntimeError('db down')
    assert await service.has_active_tasks(AsyncMock()) is False
    service.invalidate_active_tasks_cache()


# ── Слияние аккаунтов ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_keeps_one_reward_and_one_pending_per_task(monkeypatch):
    from sqlalchemy import select

    from app.database.models import RewardTask, RewardTaskSubmission, Subscription, User
    from app.services.account_merge_service import _reassign_reward_task_submissions
    from tests.fixtures.sqlite_memory import memory_session

    tables = [User.__table__, Subscription.__table__, RewardTask.__table__, RewardTaskSubmission.__table__]
    async with memory_session(monkeypatch, tables) as db:
        db.add_all([User(id=1), User(id=2), User(id=3)])
        db.add_all(
            [RewardTask(id=task_id, kind='photo_report', title=f't{task_id}', reward_days=1) for task_id in range(1, 6)]
        )
        rows = [
            # task 1: выполнено у обоих — остаётся награда primary
            (1, 1, 'approved', None),
            (1, 2, 'approved', None),
            # task 2: на проверке у обоих — второй pending лишний
            (2, 1, 'pending', None),
            (2, 2, 'pending', None),
            # task 3: primary ждёт проверки, secondary уже выполнил — pending primary не одобрить
            (3, 1, 'pending', None),
            (3, 2, 'approved', 3),
            # task 4: secondary — отклонённый и новый на проверке, проверял сам secondary (админ)
            (4, 2, 'rejected', 2),
            (4, 2, 'pending', None),
            # task 5: чужой пользователь не трогается
            (5, 3, 'approved', 2),
        ]
        db.add_all(
            [
                RewardTaskSubmission(
                    task_id=task_id,
                    user_id=user_id,
                    status=status,
                    source='bot',
                    reviewed_by=reviewer,
                    created_at=NOW,
                )
                for task_id, user_id, status, reviewer in rows
            ]
        )
        await db.commit()

        await _reassign_reward_task_submissions(db, primary_id=1, secondary_id=2)
        await db.commit()

        result = await db.execute(
            select(
                RewardTaskSubmission.task_id,
                RewardTaskSubmission.user_id,
                RewardTaskSubmission.status,
                RewardTaskSubmission.reviewed_by,
            ).order_by(RewardTaskSubmission.task_id, RewardTaskSubmission.status)
        )
        assert [tuple(row) for row in result.all()] == [
            (1, 1, 'approved', None),
            (2, 1, 'pending', None),
            (3, 1, 'approved', 3),
            (4, 1, 'pending', None),
            (4, 1, 'rejected', None),
            (5, 3, 'approved', None),
        ]
