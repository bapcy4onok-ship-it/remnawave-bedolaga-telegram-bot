"""Задания за бесплатные дни подписки (reward tasks).

Единственный источник правил и для бота, и для кабинета: кто может выполнять
задания, что считается выполнением, как начисляются дни и какие коды ошибок
видит пользователь. Хендлеры и роуты только показывают результат.

Два вида заданий:

- ``photo_report`` — пользователь присылает 1..max_photos скриншотов, админ на
  сайте одобряет или отклоняет отчёт «на честность»;
- ``channel_boost`` — бот сам проверяет бусты канала через getUserChatBoosts
  и сразу начисляет награду.

Награда — ``reward_days`` к подписке: действующая (и пробная) продлевается,
истёкшая (EXPIRED) продлевается и снова включается (решение владельца
2026-10-08). У суточного тарифа награда ещё и переносит ближайшее суточное
списание на те же дни (но не дальше новой даты окончания), а подписке без
сквадов, которую синхронизация заводит в панели заново, сквады назначаются, как
при её оформлении (см. :func:`_grant`).

Отключённую (DISABLED) подписку награда не оживляет никогда, какой бы ни была
дата окончания (ответ владельца 2026-10-09: «скрыть функцию для отключенных
подписок или забаненных людей»). Пользователю любая DISABLED показывается как
«Отключена» (``Subscription.actual_status``), так что бесплатные дни её молча
включали бы. Если наградить нечего, а отключённая подписка есть, раздел
«Задания» такому пользователю не показывается вовсе, как и заблокированному или
удалённому: ни кнопки в меню бота, ни пункта и баннера в кабинете, а его
пользовательские API отвечают ``tasks_unavailable``. Кто видит раздел, решает
:func:`get_reward_tasks_access`, единая точка для бота и кабинета. Там же
переключатель ``REWARD_TASKS_FOR_ALL_USERS``: пока он выключен, раздел видят
только админы.

Админка не меняется. Одобрение отчёта, чья подписка с тех пор стала DISABLED,
отвечает ``subscription_disabled``, отчёт заблокированного или удалённого —
``user_blocked``; в обоих случаях отчёт остаётся на проверке. Одна награда на
задание и пользователя навсегда. Гарантию даёт частичный уникальный индекс
``uq_reward_task_submissions_approved``; блокировка строки пользователя лишь
выстраивает параллельные начисления в очередь, чтобы ошибка была понятной.

Сессия: начисление коммитит сессию вызывающего (синхронизация с панелью
коммитит её изнутри), а при сбое откатывает. Откат делает истёкшими все
ORM-объекты сессии; объекты, переданные в ``check_boost``, сервис после отката
перечитывает сам, остальные вызывающему нужно перечитать до обращения к полям.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bot_factory import create_bot
from app.config import settings
from app.database.crud.required_channel import validate_channel_id
from app.database.crud.server_squad import (
    add_user_to_servers,
    get_effective_tariff_squad_uuids,
    get_random_trial_squad_uuid,
    get_server_ids_by_uuids,
)
from app.database.crud.subscription import ALIVE_SUBSCRIPTION_STATUSES, extend_subscription
from app.database.crud.tariff import get_tariff_by_id, get_trial_tariff
from app.database.models import (
    RewardTask,
    RewardTaskKind,
    RewardTaskSubmission,
    RewardTaskSubmissionSource,
    RewardTaskSubmissionStatus,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    UserStatus,
    _aware,
)
from app.external.remnawave_api import RemnaWaveAPIError
from app.localization.texts import get_texts
from app.services.subscription_service import SubscriptionService
from app.utils.cache import RateLimitCache
from app.utils.miniapp_buttons import strip_tg_emoji_tags
from app.utils.photo_sizes import pick_thumbnail_photo_size


logger = structlog.get_logger(__name__)


# ── Лимиты (их же зеркалит кабинет) ─────────────────────────────────────────

TITLE_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 3000  # описание целиком уходит в сообщение бота (лимит Telegram 4096)
REWARD_DAYS_MIN = 1
REWARD_DAYS_MAX = 365
ACTION_URL_MAX_LENGTH = 500
CHANNEL_ID_MAX_LENGTH = 100
MIN_BOOSTS_MAX = 100
MAX_PHOTOS_LIMIT = 10
SORT_ORDER_ABS_MAX = 1_000_000
MAX_COMMENT_LENGTH = 500
MAX_REJECT_REASON_LENGTH = 500
MAX_BULK_IDS = 100

DEFAULT_MIN_BOOSTS = 1
DEFAULT_MAX_PHOTOS = 3

# Проверка буста ходит в Telegram — не чаще 5 раз в минуту на пользователя.
BOOST_CHECK_RATE_LIMIT = 5
BOOST_CHECK_RATE_WINDOW_SECONDS = 60
BOOST_CHECK_RATE_ACTION = 'reward_task_boost'

# Та же граница, что у покупок (REMNAWAVE_SYNC_TIMEOUT): дольше держать блокировку
# строки пользователя ради медленной панели нельзя — начисление откатится и его
# можно повторить. Это бюджет самой синхронизации (обновление или создание).
PANEL_SYNC_TIMEOUT_SECONDS = 10.0
# Проверка «жив ли пользователь панели» (GET перед синхронизацией) идёт до неё и в
# своём коротком бюджете. В общем бюджете GET и PATCH делили бы 10 с: панель,
# отвечающая на запрос дольше ~5 с, проваливала бы каждое начисление, а брошенный
# по таймауту PATCH всё равно доезжал бы до панели, и она уходила бы вперёд базы.
# Не уложилась — ответ «неясно»: обновление, как при других неясных ответах, но
# никогда не создание. Блокировки начисления держатся до 3 + 10 ≈ 13 с.
# Бюджет — на всю проверку, а не на каждый GET, иначе с двумя id блокировки
# держались бы до 3 + 3 + 10 = 16 с. Цена: в однотарифном режиме с двумя
# известными id (пользователя и подписки) оба GET делят эти 3 с, и однозначное
# «удалён» (404 на оба) выйдет, только если панель отвечает на GET быстрее ~1,5 с.
# Медленнее — «неясно»: обновление по мёртвому id, его 404 и panel_sync_failed
# (с ERROR пересоздания после 404, известное ограничение), пока панель не ускорится.
PANEL_LOOKUP_TIMEOUT_SECONDS = 3.0
# Суточный тариф списывается, когда с прошлого списания прошли сутки
# (get_daily_subscriptions_for_charge).
DAILY_CHARGE_PERIOD = timedelta(hours=24)
NOTIFY_TIMEOUT_SECONDS = 10.0
# Уведомления шлются параллельно, но небольшими порциями — без всплеска к Bot API.
NOTIFY_CONCURRENCY = 5

# Telegram file_id — непрозрачная URL-safe base64 строка (та же проверка, что у /cabinet/media).
_FILE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{16,256}$')
_FILE_UNIQUE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_CHANNEL_USERNAME_RE = re.compile(r'^@[A-Za-z][A-Za-z0-9_]{3,31}$')
_NUMERIC_CHAT_ID_RE = re.compile(r'^-?\d+$')


class TaskUserStatus(StrEnum):
    """Состояние задания для конкретного пользователя."""

    AVAILABLE = 'available'
    PENDING = 'pending'  # отчёт на проверке
    COMPLETED = 'completed'  # награда начислена
    REJECTED = 'rejected'  # последний отчёт отклонён — можно отправить новый


class RewardTaskErrorCode(StrEnum):
    """Стабильные коды ошибок. Их видят и бот, и кабинет — не переименовывать."""

    TASK_NOT_FOUND = 'task_not_found'
    TASK_INACTIVE = 'task_inactive'
    WRONG_TASK_KIND = 'wrong_task_kind'
    ALREADY_COMPLETED = 'already_completed'
    ALREADY_PENDING = 'already_pending'
    NO_SUBSCRIPTION = 'no_subscription'
    # Наградить нечего, но есть отключённая подписка. Пользователю раздел тогда
    # не показывается (tasks_unavailable); сам код видит только админка при одобрении.
    SUBSCRIPTION_DISABLED = 'subscription_disabled'
    RESTRICTED = 'restricted'  # админ запретил пользователю покупку/продление подписки
    # Раздел скрыт для пользователя: отключённая подписка, блокировка или режим
    # «только админы» (см. get_reward_tasks_access). Кабинет отвечает 404.
    TASKS_UNAVAILABLE = 'tasks_unavailable'
    USER_BLOCKED = 'user_blocked'  # автор отчёта заблокирован или удалён — отчёт остаётся на проверке
    MEDIA_REQUIRED = 'media_required'
    TOO_MANY_MEDIA = 'too_many_media'  # params: max
    INVALID_MEDIA = 'invalid_media'
    COMMENT_TOO_LONG = 'comment_too_long'  # params: max
    TELEGRAM_REQUIRED = 'telegram_required'
    RATE_LIMITED = 'rate_limited'
    CHANNEL_CHECK_FAILED = 'channel_check_failed'
    CHANNEL_NOT_SUBSCRIBED = 'channel_not_subscribed'
    BOOST_CHECK_FAILED = 'boost_check_failed'
    NOT_ENOUGH_BOOSTS = 'not_enough_boosts'  # params: found, required
    PANEL_SYNC_FAILED = 'panel_sync_failed'
    INTERNAL_ERROR = 'internal_error'
    # Только админка
    NOT_FOUND = 'not_found'  # отчёта с таким id нет
    NOT_PENDING = 'not_pending'  # отчёт уже одобрен или отклонён
    INVALID_REASON = 'invalid_reason'
    INVALID_TASK = 'invalid_task'  # params: field
    TASK_HAS_SUBMISSIONS = 'task_has_submissions'


_ERROR_MESSAGE_KEYS: dict[str, str] = {
    RewardTaskErrorCode.TASK_NOT_FOUND: 'REWARD_TASKS_ERROR_TASK_NOT_FOUND',
    RewardTaskErrorCode.TASK_INACTIVE: 'REWARD_TASKS_ERROR_TASK_INACTIVE',
    RewardTaskErrorCode.WRONG_TASK_KIND: 'REWARD_TASKS_ERROR_WRONG_TASK_KIND',
    RewardTaskErrorCode.ALREADY_COMPLETED: 'REWARD_TASKS_ERROR_ALREADY_COMPLETED',
    RewardTaskErrorCode.ALREADY_PENDING: 'REWARD_TASKS_ERROR_ALREADY_PENDING',
    RewardTaskErrorCode.NO_SUBSCRIPTION: 'REWARD_TASKS_ERROR_NO_SUBSCRIPTION',
    RewardTaskErrorCode.SUBSCRIPTION_DISABLED: 'REWARD_TASKS_ERROR_SUBSCRIPTION_DISABLED',
    RewardTaskErrorCode.RESTRICTED: 'REWARD_TASKS_ERROR_RESTRICTED',
    RewardTaskErrorCode.TASKS_UNAVAILABLE: 'REWARD_TASKS_UNAVAILABLE',
    RewardTaskErrorCode.USER_BLOCKED: 'REWARD_TASKS_ERROR_USER_BLOCKED',
    RewardTaskErrorCode.MEDIA_REQUIRED: 'REWARD_TASKS_ERROR_MEDIA_REQUIRED',
    RewardTaskErrorCode.TOO_MANY_MEDIA: 'REWARD_TASKS_ERROR_TOO_MANY_MEDIA',
    RewardTaskErrorCode.INVALID_MEDIA: 'REWARD_TASKS_ERROR_INVALID_MEDIA',
    RewardTaskErrorCode.COMMENT_TOO_LONG: 'REWARD_TASKS_ERROR_COMMENT_TOO_LONG',
    RewardTaskErrorCode.TELEGRAM_REQUIRED: 'REWARD_TASKS_ERROR_TELEGRAM_REQUIRED',
    RewardTaskErrorCode.RATE_LIMITED: 'REWARD_TASKS_ERROR_RATE_LIMITED',
    RewardTaskErrorCode.CHANNEL_CHECK_FAILED: 'REWARD_TASKS_ERROR_CHANNEL_CHECK_FAILED',
    RewardTaskErrorCode.CHANNEL_NOT_SUBSCRIBED: 'REWARD_TASKS_ERROR_CHANNEL_NOT_SUBSCRIBED',
    RewardTaskErrorCode.BOOST_CHECK_FAILED: 'REWARD_TASKS_ERROR_BOOST_CHECK_FAILED',
    RewardTaskErrorCode.NOT_ENOUGH_BOOSTS: 'REWARD_TASKS_ERROR_NOT_ENOUGH_BOOSTS',
    RewardTaskErrorCode.PANEL_SYNC_FAILED: 'REWARD_TASKS_ERROR_PANEL_SYNC_FAILED',
    RewardTaskErrorCode.INTERNAL_ERROR: 'REWARD_TASKS_ERROR_INTERNAL',
}
_FALLBACK_MESSAGE_KEY = _ERROR_MESSAGE_KEYS[RewardTaskErrorCode.INTERNAL_ERROR]

NOTIFY_APPROVED_KEY = 'REWARD_TASKS_NOTIFY_APPROVED'
NOTIFY_REJECTED_KEY = 'REWARD_TASKS_NOTIFY_REJECTED'
# Отказ автору, которому раздел сейчас скрыт: без «можно отправить новый отчёт».
NOTIFY_REJECTED_PLAIN_KEY = 'REWARD_TASKS_NOTIFY_REJECTED_PLAIN'
NOTIFY_APPROVED_BUTTON_KEY = 'REWARD_TASKS_NOTIFY_APPROVED_BUTTON'
NOTIFY_REJECTED_BUTTON_KEY = 'REWARD_TASKS_NOTIFY_REJECTED_BUTTON'

# callback_data раздела «Задания» в боте. Живут здесь, а не в клавиатурах: кнопки
# под уведомлениями строит сервис, а импорт клавиатур отсюда дал бы цикл
# (app/keyboards/reward_tasks.py берёт эти значения у сервиса).
REWARD_TASKS_MENU_CALLBACK = 'reward_tasks_menu'
# Префиксы не пересекаются: 'reward_task_boost:1'.startswith('reward_task:') ложно.
TASK_CALLBACK_PREFIX = 'reward_task:'


class RewardTaskError(Exception):
    """Задание нельзя выполнить/обработать; ``code`` — из :class:`RewardTaskErrorCode`.

    ``params`` — значения для текста ошибки (например, ``found``/``required``
    у ``not_enough_boosts``); кабинет отдаёт их рядом с кодом.
    """

    def __init__(self, code: str, **params: Any) -> None:
        self.code = str(code)
        self.params = params
        super().__init__(self.code)


def error_message_key(code: str) -> str:
    """Ключ локали с текстом ошибки для пользователя (неизвестный код → общий текст)."""
    return _ERROR_MESSAGE_KEYS.get(code, _FALLBACK_MESSAGE_KEY)


class _FormatValues(dict):
    """Отсутствующий плейсхолдер — пустая строка, а не KeyError посреди хендлера."""

    def __missing__(self, key: str) -> str:
        return ''


def format_error_message(error: RewardTaskError | str, language: str | None = None, **params: Any) -> str:
    """Готовый текст ошибки на языке пользователя (простой текст, без HTML)."""
    if isinstance(error, RewardTaskError):
        code, values = error.code, {**error.params, **params}
    else:
        code, values = str(error), params
    texts = get_texts(language)
    template = texts.get(error_message_key(code)) or texts.get(_FALLBACK_MESSAGE_KEY) or ''
    return template.format_map(_FormatValues(values))


def format_reward_days(texts: Any, days: int) -> str:
    """«+3 дн.» / «+1 day»: в ru/ua сокращение без склонений, в en нужна форма для единицы.

    Один хелпер на экраны бота (клавиатуры импортируют его отсюда) и уведомления.
    """
    if days == 1:
        return texts.t('REWARD_TASKS_REWARD_DAY', '+{days} дн.').format(days=days)
    return texts.t('REWARD_TASKS_REWARD_DAYS', '+{days} дн.').format(days=days)


# ── Результаты ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RewardTaskView:
    """Задание глазами пользователя."""

    task: RewardTask
    status: str
    reject_reason: str | None = None
    completed_at: datetime | None = None


@dataclass(slots=True)
class RewardTasksOverview:
    eligible: bool
    ineligible_reason: str | None  # no_subscription | restricted | None (скрытым раздел не отдаётся вовсе)
    tasks: list[RewardTaskView]


@dataclass(slots=True)
class RewardGrantResult:
    submission_id: int
    task_id: int
    user_id: int
    reward_days: int
    subscription_id: int
    end_date: datetime | None


@dataclass(slots=True)
class ReviewResult:
    """Итог одного id в массовом одобрении/отклонении."""

    id: int
    ok: bool
    error_code: str | None = None


@dataclass(slots=True)
class TaskWithCounts:
    task: RewardTask
    pending_count: int
    approved_count: int


@dataclass(slots=True)
class _UserSnapshot:
    """Свежие (взятые под блокировкой) поля пользователя, нужные начислению.

    Объект ``User`` вызывающего не перечитываем через populate_existing: это
    сбросило бы его загруженные связи (``subscriptions``), и следующее обращение
    к ним в хендлере упало бы с MissingGreenlet.
    """

    id: int
    telegram_id: int | None
    language: str | None
    status: str | None
    remnawave_id: int | None
    restriction_subscription: bool


@dataclass(slots=True)
class _Notice:
    telegram_id: int
    text: str
    reply_markup: InlineKeyboardMarkup | None = None


# ── Чтение заданий ───────────────────────────────────────────────────────────


async def get_task(db: AsyncSession, task_id: int) -> RewardTask | None:
    return await db.get(RewardTask, task_id)


async def get_active_tasks(db: AsyncSession) -> list[RewardTask]:
    result = await db.execute(
        select(RewardTask).where(RewardTask.is_active.is_(True)).order_by(RewardTask.sort_order, RewardTask.id)
    )
    return list(result.scalars().all())


async def count_active_tasks(db: AsyncSession) -> int:
    result = await db.execute(select(func.count(RewardTask.id)).where(RewardTask.is_active.is_(True)))
    return int(result.scalar() or 0)


def ensure_task_usable(task: RewardTask | None, kind: str | None = None) -> RewardTask:
    """Задание существует, включено и (если указан ``kind``) нужного вида."""
    if task is None:
        raise RewardTaskError(RewardTaskErrorCode.TASK_NOT_FOUND)
    if not task.is_active:
        raise RewardTaskError(RewardTaskErrorCode.TASK_INACTIVE)
    if kind is not None and task.kind != kind:
        raise RewardTaskError(RewardTaskErrorCode.WRONG_TASK_KIND)
    return task


async def require_active_task(db: AsyncSession, task_id: int, kind: str | None = None) -> RewardTask:
    """Задание по id для пользовательских действий; иначе RewardTaskError."""
    return ensure_task_usable(await get_task(db, task_id), kind)


# ── Подписка-получатель и право выполнять задания ───────────────────────────


@dataclass(slots=True, frozen=True)
class SubscriptionCandidate:
    """Подписка-кандидат на награду: только поля, по которым идёт выбор."""

    id: int
    status: str
    end_date: datetime | None
    is_daily: bool = False


def _end_date_key(candidate: SubscriptionCandidate) -> tuple[datetime, int]:
    end = _aware(candidate.end_date) or datetime.min.replace(tzinfo=UTC)
    return end, candidate.id or 0


def choose_reward_subscription(candidates: Iterable[SubscriptionCandidate]) -> SubscriptionCandidate | None:
    """Какую подписку продлить наградой.

    Живая (active/trial/limited) с самым поздним окончанием, причём несуточная
    важнее суточной — прежнее правило выбора, его оставили. Дни на суточной уже
    не пропадают: награда переносит её ближайшее списание на те же дни (см.
    :func:`_postpone_daily_charge`), но у несуточной подаренные дни — это просто
    более поздняя дата окончания, без расчёта списаний. Нет живых — истёкшая
    (EXPIRED), которая кончилась последней: награда её продлит и снова включит
    (решение владельца 2026-10-08). Оживлённую суточную ``process_daily_charges``
    не спишет, пока не кончатся подаренные дни: награда переносит её ближайшее
    списание (иначе со старым ``last_daily_charge_at`` её списали бы сразу после
    награды).

    DISABLED не подходит никогда, какой бы ни была дата окончания (ответ
    владельца 2026-10-09). Пользователю любая DISABLED показывается как
    «Отключена» (``Subscription.actual_status``), и её отключил кто-то
    намеренно: админ («Деактивировать», «Обнулить», «Принудительная очистка»),
    блокировка, отписка от канала во время триала, синк для пользователя,
    которого нет в панели, приостановка суточного тарифа. Бесплатные дни такого
    решения не отменяют. До 2026-10-09 DISABLED с вышедшим сроком считалась
    закончившейся, как EXPIRED, и награда её оживляла; владелец это отменил.
    PENDING (неоплаченный черновик) не подходит тоже.
    """
    pool = list(candidates)

    alive = [c for c in pool if c.status in ALIVE_SUBSCRIPTION_STATUSES]
    if alive:
        return max([c for c in alive if not c.is_daily] or alive, key=_end_date_key)

    expired = [c for c in pool if c.status == SubscriptionStatus.EXPIRED.value]
    if expired:
        return max(expired, key=_end_date_key)
    return None


def no_reward_subscription_reason(candidates: Iterable[SubscriptionCandidate]) -> str:
    """Код, когда наградить нечего (:func:`choose_reward_subscription` вернула None).

    ``subscription_disabled``, если у пользователя есть хоть одна отключённая
    подписка (с любой датой окончания), иначе ``no_subscription``. Пользователю
    с ``subscription_disabled`` раздел не показывается (см.
    :func:`get_reward_tasks_access`).
    """
    if any(c.status == SubscriptionStatus.DISABLED.value for c in candidates):
        return RewardTaskErrorCode.SUBSCRIPTION_DISABLED.value
    return RewardTaskErrorCode.NO_SUBSCRIPTION.value


async def _load_subscription_candidates(
    db: AsyncSession, user_id: int, *, for_update: bool
) -> list[SubscriptionCandidate]:
    """Подписки пользователя (кроме черновиков PENDING) по свежим строкам из БД.

    Без чтения ORM-объектов из identity map. ``for_update`` блокирует строки
    подписок пользователя. Именно колонки, а не ``populate_existing`` на
    сущностях: тот сбросил бы связи (``Subscription.user``), уже загруженные
    вызывающим, и следующее обращение к ним упало бы с MissingGreenlet.
    """
    query = (
        select(Subscription.id, Subscription.status, Subscription.end_date, Tariff.is_daily)
        .outerjoin(Tariff, Tariff.id == Subscription.tariff_id)
        .where(
            Subscription.user_id == user_id,
            Subscription.status != SubscriptionStatus.PENDING.value,
        )
    )
    if for_update:
        query = query.with_for_update(of=Subscription)
    result = await db.execute(query)
    return [
        SubscriptionCandidate(id=row.id, status=row.status, end_date=row.end_date, is_daily=bool(row.is_daily))
        for row in result.all()
    ]


_SUBSCRIPTION_COLUMNS = tuple(attr.key for attr in Subscription.__mapper__.column_attrs)


async def _load_chosen_subscription(
    db: AsyncSession, chosen: SubscriptionCandidate | None, *, for_update: bool
) -> Subscription | None:
    if chosen is None:
        return None
    subscription = await db.get(Subscription, chosen.id)
    if subscription is not None and for_update:
        # Дата окончания в identity map могла устареть (пользователь только что
        # продлился другим способом), а продление считается от неё.
        await db.refresh(subscription, attribute_names=_SUBSCRIPTION_COLUMNS)
    return subscription


async def pick_reward_subscription(db: AsyncSession, user: Any, *, for_update: bool = False) -> Subscription | None:
    """Подписка, которой достанется награда, или None (некуда начислить).

    Работает и в одно-, и в мультитарифном режиме: смотрит все подписки
    пользователя. Не коммитит (в отличие от ``get_subscription_by_user_id``) —
    поэтому годится и внутри начисления. ``for_update=True`` блокирует строки
    подписок и перечитывает поля выбранной.
    """
    candidates = await _load_subscription_candidates(db, user.id, for_update=for_update)
    return await _load_chosen_subscription(db, choose_reward_subscription(candidates), for_update=for_update)


async def get_ineligible_reason(db: AsyncSession, user: User) -> str | None:
    """Почему пользователь не может выполнять задания (None — может).

    Сначала ``subscription_disabled``: наградить нечего (ни живой, ни истёкшей
    подписки, см. :func:`choose_reward_subscription`), а отключённая есть.
    Такому пользователю раздел не показывается вовсе, даже с ограничением от
    админа: у него подписка «Отключена» (ответ владельца 2026-10-09). Затем
    ограничение от админа (``restricted``). Затем ``no_subscription``, если
    наградить нечего.
    """
    candidates = await _load_subscription_candidates(db, user.id, for_update=False)
    subscription_reason = (
        None if choose_reward_subscription(candidates) is not None else no_reward_subscription_reason(candidates)
    )
    if subscription_reason == RewardTaskErrorCode.SUBSCRIPTION_DISABLED:
        return subscription_reason
    if getattr(user, 'restriction_subscription', False):
        return RewardTaskErrorCode.RESTRICTED.value
    return subscription_reason


# ── Кто видит раздел ─────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class RewardTasksAccess:
    """Видит ли пользователь раздел «Задания» и может ли выполнять задания.

    ``visible=False`` — раздела для пользователя нет: ни кнопки, ни пункта меню,
    ни баннера, а пользовательские входы отвечают ``tasks_unavailable``.
    ``ineligible_reason`` — только у видимых: ``no_subscription`` или
    ``restricted`` (раздел виден с объяснением), None — выполнять можно.
    ``hidden_reason`` — почему скрыт, для логов и тестов: ``user_blocked``,
    ``admins_only`` или ``subscription_disabled``.
    """

    visible: bool
    ineligible_reason: str | None = None
    hidden_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return self.visible and self.ineligible_reason is None


HIDDEN_ADMINS_ONLY = 'admins_only'


def is_reward_tasks_enabled_for_all_users() -> bool:
    """Переключатель ``REWARD_TASKS_FOR_ALL_USERS`` (админ-настройки бота и кабинета).

    Читается при каждом вызове: правка в настройках меняет ``settings`` в
    памяти процесса (``BotConfigurationService.set_value``) и действует сразу,
    без перезапуска и без кэша.
    """
    return bool(settings.REWARD_TASKS_FOR_ALL_USERS)


async def is_reward_tasks_admin(db: AsyncSession, user: Any) -> bool:
    """Админ для режима «только админы»: тот, кто может открыть админку.

    Тот же критерий, что у входа в админку кабинета
    (``get_current_admin_user``), один для бота и кабинета:

    - Telegram ID в ``ADMIN_IDS`` (это и админы бота: ``settings.is_admin``
      открывает им «Админ-панель»);
    - e-mail в ``ADMIN_EMAILS``, подтверждённый доверенным способом
      (``is_user_admin_by_env``);
    - активная, не истёкшая RBAC-роль уровня выше 0 (роли кабинета:
      Superadmin, Admin, Moderator, Marketer, Support и свои).

    Модераторы поддержки бота (``SupportSettingsService``) админами не
    считаются: админку они не открывают. Без SQL для админов из окружения,
    иначе один запрос ролей.
    """
    from app.database.crud.rbac import UserRoleCRUD
    from app.services.rbac_bootstrap_service import is_user_admin_by_env

    if is_user_admin_by_env(user).is_admin:
        return True
    _permissions, _role_names, max_level = await UserRoleCRUD.get_user_permissions(db, user.id)
    return max_level > 0


async def get_reward_tasks_access(db: AsyncSession, user: Any) -> RewardTasksAccess:
    """Единственный источник правды о том, видит ли пользователь раздел «Задания».

    По ней решают кнопка главного меню бота (оба пути: конструктор меню и
    обычная клавиатура), ``/start tasks`` и ``/tasks``, каждая кнопка и сбор
    скриншотов в боте, все пользовательские роуты кабинета, включая счётчик для
    пункта меню и баннера. Порядок:

    1. Заблокированный или удалённый — скрыт (``user_blocked``).
    2. ``REWARD_TASKS_FOR_ALL_USERS`` выключен, а пользователь не админ
       (:func:`is_reward_tasks_admin`) — скрыт (``admins_only``). Админы при
       выключенном переключателе проходят дальше, как все при включённом.
    3. Наградить нечего, а отключённая подписка есть — скрыт
       (``subscription_disabled``, ответ владельца 2026-10-09).
    4. Иначе виден, с причиной ``restricted`` / ``no_subscription`` или без неё.

    Запросы: роли (только не-админам из окружения при выключенном
    переключателе) и подписки пользователя. Не коммитит.
    """
    if user is None:
        return RewardTasksAccess(visible=False, hidden_reason=RewardTaskErrorCode.USER_BLOCKED.value)
    # NULL — старые строки до дефолта колонки; бот считает их активными (как _lock_user).
    status = getattr(user, 'status', None) or UserStatus.ACTIVE.value
    if status != UserStatus.ACTIVE.value:
        return RewardTasksAccess(visible=False, hidden_reason=RewardTaskErrorCode.USER_BLOCKED.value)
    if not is_reward_tasks_enabled_for_all_users() and not await is_reward_tasks_admin(db, user):
        return RewardTasksAccess(visible=False, hidden_reason=HIDDEN_ADMINS_ONLY)
    reason = await get_ineligible_reason(db, user)
    if reason == RewardTaskErrorCode.SUBSCRIPTION_DISABLED:
        return RewardTasksAccess(visible=False, hidden_reason=reason)
    return RewardTasksAccess(visible=True, ineligible_reason=reason)


async def require_reward_tasks_access(db: AsyncSession, user: Any) -> RewardTasksAccess:
    """Доступ видимого пользователя; скрытому — ``RewardTaskError('tasks_unavailable')``."""
    access = await get_reward_tasks_access(db, user)
    if not access.visible:
        raise RewardTaskError(RewardTaskErrorCode.TASKS_UNAVAILABLE)
    return access


async def _ensure_eligible(db: AsyncSession, user: User) -> None:
    """Перед отправкой отчёта и проверкой буста: раздел виден и выполнять задания можно."""
    access = await require_reward_tasks_access(db, user)
    if access.ineligible_reason is not None:
        raise RewardTaskError(access.ineligible_reason)


# ── Статусы заданий пользователя ─────────────────────────────────────────────


async def _load_user_submissions(db: AsyncSession, user_id: int, task_ids: Sequence[int]) -> list[RewardTaskSubmission]:
    if not task_ids:
        return []
    result = await db.execute(
        select(RewardTaskSubmission)
        .where(
            RewardTaskSubmission.user_id == user_id,
            RewardTaskSubmission.task_id.in_(list(task_ids)),
        )
        .order_by(RewardTaskSubmission.created_at.desc(), RewardTaskSubmission.id.desc())
    )
    return list(result.scalars().all())


def resolve_task_view(task: RewardTask, submissions: Sequence[RewardTaskSubmission]) -> RewardTaskView:
    """Статус задания по отчётам пользователя (``submissions`` — новые первыми)."""
    approved = next((s for s in submissions if s.status == RewardTaskSubmissionStatus.APPROVED), None)
    if approved is not None:
        return RewardTaskView(
            task=task,
            status=TaskUserStatus.COMPLETED.value,
            completed_at=approved.reviewed_at or approved.created_at,
        )
    if any(s.status == RewardTaskSubmissionStatus.PENDING for s in submissions):
        return RewardTaskView(task=task, status=TaskUserStatus.PENDING.value)
    latest = submissions[0] if submissions else None
    if latest is not None and latest.status == RewardTaskSubmissionStatus.REJECTED:
        return RewardTaskView(task=task, status=TaskUserStatus.REJECTED.value, reject_reason=latest.reject_reason)
    return RewardTaskView(task=task, status=TaskUserStatus.AVAILABLE.value)


async def list_tasks_for_user(
    db: AsyncSession, user: User, *, access: RewardTasksAccess | None = None
) -> RewardTasksOverview:
    """Активные задания (по sort_order, id) со статусом для пользователя и его право их выполнять.

    ``access`` — уже посчитанный :func:`get_reward_tasks_access` (чтобы не
    запрашивать второй раз). Скрытому пользователю списка нет:
    ``RewardTaskError('tasks_unavailable')``.
    """
    if access is None:
        access = await get_reward_tasks_access(db, user)
    if not access.visible:
        raise RewardTaskError(RewardTaskErrorCode.TASKS_UNAVAILABLE)
    tasks = await get_active_tasks(db)
    submissions = await _load_user_submissions(db, user.id, [task.id for task in tasks])
    by_task: dict[int, list[RewardTaskSubmission]] = defaultdict(list)
    for submission in submissions:
        by_task[submission.task_id].append(submission)

    return RewardTasksOverview(
        eligible=access.ineligible_reason is None,
        ineligible_reason=access.ineligible_reason,
        tasks=[resolve_task_view(task, by_task.get(task.id, [])) for task in tasks],
    )


async def get_task_view(db: AsyncSession, user: User, task_id: int) -> RewardTaskView | None:
    """Одно активное задание со статусом пользователя (для экрана задания в боте)."""
    task = await get_task(db, task_id)
    if task is None or not task.is_active:
        return None
    submissions = await _load_user_submissions(db, user.id, [task.id])
    return resolve_task_view(task, submissions)


async def _has_submission(db: AsyncSession, task_id: int, user_id: int, status: str) -> bool:
    result = await db.execute(
        select(RewardTaskSubmission.id)
        .where(
            RewardTaskSubmission.task_id == task_id,
            RewardTaskSubmission.user_id == user_id,
            RewardTaskSubmission.status == status,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


# ── Видимость кнопки в меню бота ─────────────────────────────────────────────

_ACTIVE_TASKS_CACHE_TTL_SECONDS = 60.0
_active_tasks_cache: tuple[float, bool] | None = None


async def has_active_tasks(db: AsyncSession) -> bool:
    """Есть ли включённые задания — первая половина условия ``reward_tasks_visible`` меню.

    Вторая — :func:`get_reward_tasks_access` для конкретного пользователя: она
    не кэшируется, поэтому переключатель «для всех» и отключение подписки
    действуют сразу. Кэш здесь только на «есть ли задания вообще»: на минуту в
    памяти процесса (меню строится на каждый показ, а задания меняются редко);
    админские правки заданий сбрасывают его сразу. Никогда не бросает.
    """
    global _active_tasks_cache
    now = time.monotonic()
    cached = _active_tasks_cache
    if cached is not None and now - cached[0] < _ACTIVE_TASKS_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        visible = await count_active_tasks(db) > 0
    except Exception as error:
        logger.warning('Не удалось проверить наличие активных заданий', error=str(error))
        return cached[1] if cached is not None else False
    _active_tasks_cache = (now, visible)
    return visible


def invalidate_active_tasks_cache() -> None:
    global _active_tasks_cache
    _active_tasks_cache = None


# ── Фото ─────────────────────────────────────────────────────────────────────


def build_photo_media_item(photo_sizes: Sequence[Any]) -> dict[str, Any] | None:
    """Элемент ``media_items`` из ``Message.photo``: полный размер + превью ~320px."""
    if not photo_sizes:
        return None
    full = photo_sizes[-1]
    thumbnail = pick_thumbnail_photo_size(photo_sizes)
    return {
        'type': 'photo',
        'file_id': full.file_id,
        'file_unique_id': getattr(full, 'file_unique_id', None),
        'thumb_file_id': thumbnail.file_id if thumbnail is not None else None,
    }


def _media_value(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def normalize_media_items(items: Iterable[Any] | None, max_items: int) -> list[dict[str, Any]]:
    """Проверяет скриншоты отчёта и приводит их к формату ``media_items``.

    Принимает dict, pydantic-модели и объекты с атрибутами ``file_id``,
    ``file_unique_id``, ``thumb_file_id``. Повторы одного файла схлопываются
    (альбом мог прийти дважды).
    """
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or ():
        file_id = _media_value(item, 'file_id')
        if not isinstance(file_id, str) or not _FILE_ID_RE.match(file_id):
            raise RewardTaskError(RewardTaskErrorCode.INVALID_MEDIA)

        unique_id = _media_value(item, 'file_unique_id') or None
        if unique_id is not None and (not isinstance(unique_id, str) or not _FILE_UNIQUE_ID_RE.match(unique_id)):
            raise RewardTaskError(RewardTaskErrorCode.INVALID_MEDIA)

        thumb_id = _media_value(item, 'thumb_file_id') or None
        if thumb_id is not None and (not isinstance(thumb_id, str) or not _FILE_ID_RE.match(thumb_id)):
            raise RewardTaskError(RewardTaskErrorCode.INVALID_MEDIA)

        dedupe_key = unique_id or file_id
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append({'type': 'photo', 'file_id': file_id, 'file_unique_id': unique_id, 'thumb_file_id': thumb_id})

    if not normalized:
        raise RewardTaskError(RewardTaskErrorCode.MEDIA_REQUIRED)
    if len(normalized) > max_items:
        raise RewardTaskError(RewardTaskErrorCode.TOO_MANY_MEDIA, max=max_items)
    return normalized


def _normalize_comment(comment: str | None) -> str | None:
    text = (comment or '').strip()
    if not text:
        return None
    if len(text) > MAX_COMMENT_LENGTH:
        raise RewardTaskError(RewardTaskErrorCode.COMMENT_TOO_LONG, max=MAX_COMMENT_LENGTH)
    return text


# ── Фотоотчёт ────────────────────────────────────────────────────────────────


async def submit_photo_report(
    db: AsyncSession,
    user: User,
    task: RewardTask,
    media_items: Iterable[Any],
    comment: str | None = None,
    source: str = RewardTaskSubmissionSource.BOT,
) -> RewardTaskSubmission:
    """Создаёт отчёт на проверку (status=pending) и коммитит.

    ``media_items`` — dict/модели с ``file_id`` и необязательными
    ``file_unique_id``/``thumb_file_id`` (для фото из бота см.
    :func:`build_photo_media_item`). ``source`` — ``bot`` или ``cabinet``.
    Скрытому разделу (:func:`get_reward_tasks_access`) — ``tasks_unavailable``.
    """
    if source not in (RewardTaskSubmissionSource.BOT, RewardTaskSubmissionSource.CABINET):
        raise ValueError(f'Unsupported submission source: {source!r}')

    ensure_task_usable(task, RewardTaskKind.PHOTO_REPORT)
    task_id = task.id
    user_id = user.id
    if await _has_submission(db, task_id, user_id, RewardTaskSubmissionStatus.APPROVED):
        raise RewardTaskError(RewardTaskErrorCode.ALREADY_COMPLETED)
    if await _has_submission(db, task_id, user_id, RewardTaskSubmissionStatus.PENDING):
        raise RewardTaskError(RewardTaskErrorCode.ALREADY_PENDING)
    await _ensure_eligible(db, user)
    items = normalize_media_items(media_items, max_items=task.max_photos)
    text = _normalize_comment(comment)

    submission = RewardTaskSubmission(
        task_id=task_id,
        user_id=user_id,
        status=RewardTaskSubmissionStatus.PENDING.value,
        source=str(source),
        media_items=items,
        comment=text,
        created_at=datetime.now(UTC),
    )
    try:
        # Двойное нажатие «Готово»: проверка выше пропустила оба запроса,
        # вставку второго отбивает uq_reward_task_submissions_pending.
        # Savepoint — чтобы откат не задел остальные объекты сессии.
        async with db.begin_nested():
            db.add(submission)
            await db.flush()
    except IntegrityError:
        raise RewardTaskError(RewardTaskErrorCode.ALREADY_PENDING) from None
    await db.commit()

    logger.info(
        '📸 Отправлен фотоотчёт по заданию',
        submission_id=submission.id,
        task_id=task_id,
        user_id=user_id,
        photos=len(items),
        source=str(source),
    )
    return submission


# ── Буст канала ──────────────────────────────────────────────────────────────


def count_active_boosts(boosts: Iterable[Any], now: datetime | None = None) -> int:
    """Сколько бустов ещё не истекло (``expiration_date`` в будущем)."""
    moment = now or datetime.now(UTC)
    active = 0
    for boost in boosts or ():
        expires = getattr(boost, 'expiration_date', None)
        if isinstance(expires, int | float):
            expires = datetime.fromtimestamp(expires, UTC)
        if not isinstance(expires, datetime):
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires > moment:
            active += 1
    return active


def _chat_id_for_api(channel_id: str | None) -> int | str | None:
    value = (channel_id or '').strip()
    if not value:
        return None
    if _NUMERIC_CHAT_ID_RE.match(value):
        return int(value)
    return value


async def _fetch_active_boosts(bot: Any, task: RewardTask, telegram_id: int) -> int:
    chat_id = _chat_id_for_api(task.channel_id)
    if chat_id is None:
        logger.error('У задания на буст не указан канал', task_id=task.id)
        raise RewardTaskError(RewardTaskErrorCode.BOOST_CHECK_FAILED)
    try:
        result = await bot.get_user_chat_boosts(chat_id=chat_id, user_id=telegram_id)
    except Exception as error:
        from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

        # BadRequest/Forbidden почти всегда значат «бот не админ канала или канал
        # указан неверно» — это чинит админ, поэтому error. Остальное (сеть,
        # флуд-контроль) — транзиент.
        log = logger.error if isinstance(error, TelegramBadRequest | TelegramForbiddenError) else logger.warning
        log(
            'Не удалось получить бусты пользователя',
            task_id=task.id,
            channel_id=task.channel_id,
            telegram_id=telegram_id,
            error_type=type(error).__name__,
            error=str(error),
        )
        raise RewardTaskError(RewardTaskErrorCode.BOOST_CHECK_FAILED) from None
    return count_active_boosts(getattr(result, 'boosts', None) or [])


async def check_boost(
    db: AsyncSession,
    user: User,
    task: RewardTask,
    bot: Any,
    *,
    notify: bool = False,
) -> RewardGrantResult:
    """Проверяет буст канала и сразу начисляет награду.

    Уведомление по умолчанию не шлётся: и бот, и кабинет показывают результат
    сами, второе сообщение было бы дублем. После ошибки начисления ``user`` и
    ``task`` перечитываются — ими можно пользоваться дальше. Скрытому разделу
    (:func:`get_reward_tasks_access`) — ``tasks_unavailable`` до запроса в Telegram.
    """
    ensure_task_usable(task, RewardTaskKind.CHANNEL_BOOST)
    task_id = task.id
    user_id = user.id
    telegram_id = user.telegram_id
    if not telegram_id:
        raise RewardTaskError(RewardTaskErrorCode.TELEGRAM_REQUIRED)
    if await _has_submission(db, task_id, user_id, RewardTaskSubmissionStatus.APPROVED):
        raise RewardTaskError(RewardTaskErrorCode.ALREADY_COMPLETED)
    await _ensure_eligible(db, user)

    if await RateLimitCache.is_rate_limited(
        user_id,
        BOOST_CHECK_RATE_ACTION,
        limit=BOOST_CHECK_RATE_LIMIT,
        window=BOOST_CHECK_RATE_WINDOW_SECONDS,
    ):
        raise RewardTaskError(RewardTaskErrorCode.RATE_LIMITED)

    found = await _fetch_active_boosts(bot, task, telegram_id)
    required = task.min_boosts or DEFAULT_MIN_BOOSTS
    if found < required:
        raise RewardTaskError(RewardTaskErrorCode.NOT_ENOUGH_BOOSTS, found=found, required=required)

    submission = RewardTaskSubmission(
        task_id=task_id,
        user_id=user_id,
        status=RewardTaskSubmissionStatus.PENDING.value,
        source=RewardTaskSubmissionSource.AUTO.value,
        media_items=[],
        boost_count=found,
        created_at=datetime.now(UTC),
    )
    result, notice = await _grant(db, submission, reviewer_id=None, restore=(user, task))
    if notify:
        await _deliver_notices([notice], bot=bot)
    return result


async def check_channel_subscription(
    db: AsyncSession,
    user: User,
    task: RewardTask,
    bot: Any,
    *,
    notify: bool = False,
) -> RewardGrantResult:
    """Проверяет подписку на канал и однократно начисляет дни.

    Уведомление по умолчанию не шлётся: и бот, и кабинет показывают результат
    сами, второе сообщение было бы дублем. После ошибки начисления ``user`` и
    ``task`` перечитываются — ими можно пользоваться дальше. Скрытому разделу
    (:func:`get_reward_tasks_access`) — ``tasks_unavailable`` до запроса в Telegram.
    """
    ensure_task_usable(task, RewardTaskKind.CHANNEL_SUBSCRIPTION)
    task_id = task.id
    user_id = user.id
    telegram_id = user.telegram_id
    if not telegram_id:
        raise RewardTaskError(RewardTaskErrorCode.TELEGRAM_REQUIRED)
    if await _has_submission(db, task_id, user_id, RewardTaskSubmissionStatus.APPROVED):
        raise RewardTaskError(RewardTaskErrorCode.ALREADY_COMPLETED)
    await _ensure_eligible(db, user)

    if await RateLimitCache.is_rate_limited(
        user_id,
        'reward_task_channel_subscription',
        limit=BOOST_CHECK_RATE_LIMIT,
        window=BOOST_CHECK_RATE_WINDOW_SECONDS,
    ):
        raise RewardTaskError(RewardTaskErrorCode.RATE_LIMITED)

    from app.services.trial_channel_service import is_channel_member

    try:
        member = await bot.get_chat_member(chat_id=_chat_id_for_api(task.channel_id), user_id=telegram_id)
    except Exception as error:
        logger.warning('Channel reward membership check failed', task_id=task_id, error_type=type(error).__name__)
        raise RewardTaskError(RewardTaskErrorCode.CHANNEL_CHECK_FAILED) from None
    if not is_channel_member(member):
        raise RewardTaskError(RewardTaskErrorCode.CHANNEL_NOT_SUBSCRIBED)

    submission = RewardTaskSubmission(
        task_id=task_id,
        user_id=user_id,
        status=RewardTaskSubmissionStatus.PENDING.value,
        source=RewardTaskSubmissionSource.AUTO.value,
        media_items=[],
        created_at=datetime.now(UTC),
    )
    result, notice = await _grant(db, submission, reviewer_id=None, restore=(user, task))
    if notify:
        await _deliver_notices([notice], bot=bot)
    return result


# ── Начисление ───────────────────────────────────────────────────────────────


async def grant(
    db: AsyncSession,
    submission: RewardTaskSubmission,
    reviewer: User | None = None,
    *,
    bot: Any = None,
    notify: bool = True,
) -> RewardGrantResult:
    """Начисляет награду по отчёту (атомарно с синхронизацией панели) и уведомляет пользователя.

    ``submission`` — отчёт в статусе pending либо ещё не сохранённая
    ``auto``-заявка. При любом сбое ничего не начислено, отчёт остаётся pending.
    """
    reviewer_id = reviewer.id if reviewer is not None else None
    result, notice = await _grant(db, submission, reviewer_id=reviewer_id)
    if notify:
        await _deliver_notices([notice], bot=bot)
    return result


async def _lock_user(db: AsyncSession, user_id: int) -> _UserSnapshot | None:
    result = await db.execute(
        select(
            User.id,
            User.telegram_id,
            User.language,
            User.status,
            User.remnawave_id,
            User.restriction_subscription,
        )
        .where(User.id == user_id)
        .with_for_update()
    )
    row = result.one_or_none()
    if row is None:
        return None
    return _UserSnapshot(
        id=row.id,
        telegram_id=row.telegram_id,
        language=row.language,
        # NULL — старые строки до дефолта колонки; бот считает их активными.
        status=row.status or UserStatus.ACTIVE.value,
        remnawave_id=row.remnawave_id,
        restriction_subscription=bool(row.restriction_subscription),
    )


async def _lock_submission(db: AsyncSession, submission_id: int) -> RewardTaskSubmission | None:
    result = await db.execute(
        select(RewardTaskSubmission)
        .where(RewardTaskSubmission.id == submission_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _safe_rollback(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception as error:
        logger.warning('Не удалось откатить сессию', error=str(error))


async def _restore(db: AsyncSession, instances: Iterable[Any]) -> None:
    """После отката все объекты сессии истекли — перечитываем те, что ещё нужны вызывающему."""
    for instance in instances:
        if instance is None:
            continue
        try:
            if instance in db:
                await db.refresh(instance)
        except Exception as error:
            logger.debug('Не удалось перечитать объект после отката', error=str(error))


async def _release(db: AsyncSession) -> None:
    """Отпускает блокировки, когда начислять нечего: commit не делает объекты истёкшими."""
    try:
        await db.commit()
    except Exception as error:
        logger.warning('Не удалось завершить транзакцию задания', error=str(error))
        await _safe_rollback(db)


async def _fail(db: AsyncSession, restore: Iterable[Any], code: str) -> RewardTaskError:
    await _safe_rollback(db)
    await _restore(db, restore)
    return RewardTaskError(code)


def _panel_ids_for_update(subscription: Subscription, user: _UserSnapshot) -> tuple[int, ...]:
    """Панельные id, которыми бот адресует подписку; пусто — синхронизировать созданием.

    Первый — ровно тот, что возьмёт ``update_remnawave_user``: в мультитарифе
    id подписки, в однотарифном — id пользователя. Нет его — пусто, как и
    раньше: ``create_remnawave_user`` сам найдёт аккаунт (по id подписки,
    shortUuid, telegram id) или создаст. В однотарифном режиме к нему
    добавляется id самой подписки, если он другой: при мёртвом id пользователя
    ``validate_and_clean_subscription`` стёр бы и его (на фолбэк к id подписки
    он смотрит, только когда у пользователя id нет), и find-or-create мог бы
    завести дубль рядом с живым аккаунтом этой подписки.
    """
    if settings.is_multi_tariff_enabled():
        return () if subscription.remnawave_id is None else (subscription.remnawave_id,)
    if user.remnawave_id is None:
        return ()
    if subscription.remnawave_id is None or subscription.remnawave_id == user.remnawave_id:
        return (user.remnawave_id,)
    return user.remnawave_id, subscription.remnawave_id


async def _panel_user_deleted(service: SubscriptionService, panel_ids: Sequence[int], *, subscription_id: int) -> bool:
    """True — только если панель на каждый из ``panel_ids`` ответила 404 (пользователя удалили).

    ``get_user_by_id`` отдаёт None лишь на 404; таймаут, 5xx, 429, «панель не
    настроена» бросают — это «не знаем», а не «нет», и ответ тогда False. Свой
    бюджет — ``PANEL_LOOKUP_TIMEOUT_SECONDS``: не уложилась — тоже «не знаем».
    Не бросает. Отмену снаружи не глотает: ``asyncio.timeout`` превращает в
    TimeoutError только собственную, а CancelledError — не Exception.
    """
    try:
        async with asyncio.timeout(PANEL_LOOKUP_TIMEOUT_SECONDS):
            async with service.get_api_client() as api:
                for panel_id in panel_ids:
                    if await api.get_user_by_id(panel_id) is not None:
                        return False
    except (TimeoutError, RemnaWaveAPIError) as error:
        logger.warning(
            'Не удалось проверить пользователя панели перед начислением награды',
            subscription_id=subscription_id,
            error_type=type(error).__name__,
            error=str(error),
        )
        return False
    except Exception as error:
        logger.exception(
            'Сбой проверки пользователя панели перед начислением награды',
            subscription_id=subscription_id,
            error_type=type(error).__name__,
        )
        return False
    logger.warning(
        'Пользователя панели нет (404) — награда за задание синхронизируется созданием',
        subscription_id=subscription_id,
        remnawave_ids=list(panel_ids),
    )
    return True


@dataclass(slots=True, frozen=True)
class _MissingSquads:
    """Сквады для подписки, у которой их нет: найдены до синхронизации, простыми значениями.

    Назначаются только при синхронизации созданием (см. :func:`_sync_panel`).
    """

    squads: tuple[str, ...]  # пусто — назначить нечего: в базе нет ни одного доступного сквада
    server_ids: tuple[int, ...]  # их строки в server_squads — для счётчиков пользователей серверов
    source: str  # откуда взяты (для лога): tariff | trial_tariff | trial_squad | all_available
    # Триал без тарифа, а сквадов для триала нет: все доступные, как у
    # create_trial_subscription по умолчанию, — об этом предупреждаем.
    no_trial_squads: bool = False


async def _trial_tariff(db: AsyncSession) -> Tariff | None:
    """Тариф триала — тот же выбор, что при активации триала в боте, кабинете и по промокоду."""
    tariff = await get_trial_tariff(db)
    trial_tariff_id = settings.get_trial_tariff_id()
    if tariff is None and trial_tariff_id > 0:
        tariff = await get_tariff_by_id(db, trial_tariff_id, with_promo_groups=False)
    return tariff


async def _resolve_missing_squads(db: AsyncSession, subscription: Subscription) -> _MissingSquads | None:
    """Сквады для подписки без сквадов; None — сквады у неё есть.

    Истёкшая (EXPIRED) подписка без сквадов остаётся, например, после вебхука
    ``user.deleted`` (подписка → EXPIRED, сквады и панельный id стёрты) или синка
    из панели, где у пользователя нет сквадов. Оживлённую наградой такую подписку
    синхронизация может завести в панели заново (панельного id нет или панель
    ответила 404) — без сквадов это пользователь без серверов. Строки, которые
    стирают «Принудительная очистка», синк для отсутствующих в панели и
    «Обнулить», — DISABLED: их награда не оживляет вовсе (ответ владельца
    2026-10-09), сюда они не доходят. Сквады — так, как их получает такая
    подписка в остальном коде, через ``get_effective_tariff_squad_uuids``
    (пустой список — все доступные):

    - подписка с тарифом — сквады тарифа (как у купонов и суточного списания);
    - триал без тарифа — ровно как при активации триала (бот, кабинет,
      ``/start``): сквады триального тарифа, если он задан, иначе один случайный
      сквад для триалов (``get_random_trial_squad_uuid``) — оживлённый триал не
      получает больше серверов, чем новый; нет и таких — все доступные, как
      ``create_trial_subscription`` по умолчанию (с предупреждением в лог);
    - остальные — все доступные.
    """
    if subscription.connected_squads:
        return None
    allowed: Sequence[str] | None = None
    source = 'all_available'
    no_trial_squads = False
    if subscription.tariff_id is not None:
        result = await db.execute(select(Tariff.allowed_squads).where(Tariff.id == subscription.tariff_id))
        allowed = result.scalar_one_or_none()
        source = 'tariff'
    elif subscription.is_trial:
        trial_tariff = await _trial_tariff(db)
        if trial_tariff is not None:
            allowed = trial_tariff.allowed_squads
            source = 'trial_tariff'
        elif trial_squad := await get_random_trial_squad_uuid(db):
            allowed = [trial_squad]
            source = 'trial_squad'
        else:
            no_trial_squads = True
    squads = tuple(await get_effective_tariff_squad_uuids(db, allowed))
    server_ids = tuple(await get_server_ids_by_uuids(db, list(squads))) if squads else ()
    return _MissingSquads(
        squads=squads,
        server_ids=server_ids,
        source=source if allowed else 'all_available',
        no_trial_squads=no_trial_squads,
    )


async def _assign_missing_squads(
    db: AsyncSession, subscription: Subscription, missing: _MissingSquads | None, *, subscription_id: int
) -> None:
    """Перед созданием пользователя панели — сквады подписке без сквадов, в той же транзакции.

    Счётчики пользователей серверов растут, как при оживлении и создании
    подписок со сквадами (``add_user_to_servers``). Сбой синхронизации
    откатывает и то и другое: подписка снова без сквадов.
    """
    if missing is None:
        return
    if not missing.squads:
        # Как при активации триала: пользователь создаётся и без сквадов, но админ должен узнать.
        logger.error(
            'Подписке без сквадов нечего назначить: в базе нет доступных сквадов, пользователь панели будет без серверов',
            subscription_id=subscription_id,
        )
        return
    if missing.no_trial_squads:
        # Сквадов для триалов не настроено: как и новый триал (create_trial_subscription),
        # получает все доступные — но обычно у триала один сервер, админ должен знать.
        logger.warning(
            'Сквадов для выдачи триалов нет — оживлённому триалу назначены все доступные сквады',
            subscription_id=subscription_id,
            squads=list(missing.squads),
        )
    subscription.connected_squads = list(missing.squads)
    if missing.server_ids:
        await add_user_to_servers(db, list(missing.server_ids))
    await db.flush()
    logger.info(
        'Подписке без сквадов назначены сквады перед созданием пользователя панели',
        subscription_id=subscription_id,
        squads=list(missing.squads),
        source=missing.source,
    )


async def _sync_panel(
    db: AsyncSession,
    subscription: Subscription,
    *,
    subscription_id: int,
    panel_ids: Sequence[int],
    missing_squads: _MissingSquads | None = None,
) -> bool:
    """Пушит продление в Remnawave. True — панель приняла изменения. Не бросает.

    Есть панельный id — ``update_remnawave_user``: только он завершает открытую
    grace-сессию (``apply_recovered_grace_update_locked``), а оживление истёкших
    — ровно её случай (в проде GRACE_ACCESS_MODE включён). Но пользователя могли
    удалить в панели, а вебхуки в проде выключены, и бот хранит мёртвый id:
    обновление получает 404, откатывает сессию вместе с начислением и не
    пересоздаёт неактивную подписку — награда не начислилась бы никогда.
    Поэтому сначала спрашиваем панель, жив ли этот id, и только на однозначное
    «нет» (404) синхронизируем через ``create_remnawave_user``, как купоны:
    find-or-create сам перепроверит id, сотрёт мёртвую привязку в нашей
    транзакции, создаст пользователя с уже продлённым сроком и закоммитит.
    Любой другой ответ (пользователь есть, ошибка, таймаут запроса или бюджета
    проверки, неожиданный сбой) — обновление, как раньше: дубль рядом с,
    возможно, живым оригиналом недопустим. Нет панельного id — сразу
    ``create_remnawave_user``.

    Бюджеты последовательные: у проверки свой (``PANEL_LOOKUP_TIMEOUT_SECONDS``),
    синхронизация (обновление или создание) после неё получает весь
    ``PANEL_SYNC_TIMEOUT_SECONDS``. Внутри проверки к базе не ходим.

    Перед созданием подписке без сквадов назначаются ``missing_squads`` (см.
    :func:`_resolve_missing_squads`), иначе пользователь панели был бы без
    серверов. Обновление сквады не трогает: у живого пользователя панели они
    свои.

    Оба метода SubscriptionService коммитят сессию при успехе и откатывают при
    ошибке API, возвращая None; ``update`` без панельного id возвращает None
    без отката — поэтому вызывающий в любом случае откатывает сам.

    Таймаут (CancelledError внутри) и 404 → пересоздание откатывают сессию и
    бросают: откат делает истёкшими все ORM-объекты, и чтение их полей дальше
    падает с MissingGreenlet (так и падает пересоздание после 404, читая
    ``subscription.status``). Поэтому после вызова к ORM не обращаемся — в лог
    идут простые значения, а любое исключение превращается в False: итог
    начисления вызывающий определяет по базе. Таймаут и ошибки API — штатные
    сбои панели (warning без трейсбека); всё прочее — ошибка в коде, она идёт
    в error с трейсбеком, а значит, и в админ-чат.
    """
    try:
        service = SubscriptionService()
        create = not panel_ids or await _panel_user_deleted(service, panel_ids, subscription_id=subscription_id)
        async with asyncio.timeout(PANEL_SYNC_TIMEOUT_SECONDS):
            if create:
                await _assign_missing_squads(db, subscription, missing_squads, subscription_id=subscription_id)
                panel_user = await service.create_remnawave_user(db, subscription)
            else:
                panel_user = await service.update_remnawave_user(db, subscription)
    except (TimeoutError, RemnaWaveAPIError) as error:
        logger.warning(
            'Синхронизация награды за задание с панелью не удалась',
            subscription_id=subscription_id,
            error_type=type(error).__name__,
            error=str(error),
        )
        return False
    except Exception as error:
        logger.exception(
            'Синхронизация награды за задание с панелью упала',
            subscription_id=subscription_id,
            error_type=type(error).__name__,
        )
        return False
    return panel_user is not None


async def _approved_in_db(db: AsyncSession, submission_id: int) -> bool:
    result = await db.execute(select(RewardTaskSubmission.status).where(RewardTaskSubmission.id == submission_id))
    return result.scalar_one_or_none() == RewardTaskSubmissionStatus.APPROVED


async def _grant(
    db: AsyncSession,
    submission: RewardTaskSubmission,
    *,
    reviewer_id: int | None,
    restore: Sequence[Any] = (),
) -> tuple[RewardGrantResult, _Notice | None]:
    """Атомарное начисление по образцу ``coupon_service.redeem_coupon``.

    1. Блокировка строки пользователя и (для существующего отчёта) строки отчёта,
       повторные проверки под блокировкой: пользователь не заблокирован, задание
       не выполнено, нет ограничения, есть подписка, которой можно начислить дни
       (живая или истёкшая, но не DISABLED — :func:`choose_reward_subscription`).
       Переключатель «только админы» здесь не проверяется: одобрение — дело
       админки, а буст проверяет видимость раздела до начисления.
    2. Выбор подписки ДО любых изменений — ничего коммитящего после этого.
    3. Отчёт → approved и flush (гонку мимо блокировки ловит уникальный индекс),
       затем ``extend_subscription(commit=False)``, у суточного тарифа — перенос
       ближайшего списания (:func:`_postpone_daily_charge`), и поиск сквадов для
       подписки без сквадов (:func:`_resolve_missing_squads`) — всё в той же
       транзакции и до синхронизации.
    4. Синхронизация с панелью — она и коммитит (обновлением, а если
       пользователя в панели удалили — созданием, см. :func:`_sync_panel`).
       Любой её сбой → откат, отчёт остаётся pending, код ``panel_sync_failed``.
    5. Итог определяет база: синхронизация могла откатить сессию изнутри
       (404 → пересоздание пользователя панели) и всё равно вернуть успех — или
       упасть уже после своего коммита.
    """
    is_new = submission.id is None
    submission_id = submission.id
    task_id = submission.task_id
    user_id = submission.user_id

    try:
        user = await _lock_user(db, user_id)
        if user is None:
            raise RewardTaskError(RewardTaskErrorCode.NOT_FOUND)
        if not is_new:
            locked = await _lock_submission(db, submission_id)
            if locked is None:
                raise RewardTaskError(RewardTaskErrorCode.NOT_FOUND)
            if locked.status != RewardTaskSubmissionStatus.PENDING:
                raise RewardTaskError(RewardTaskErrorCode.NOT_PENDING)
            submission = locked
        if user.status != UserStatus.ACTIVE.value:
            # Заблокировали после отправки отчёта: одобрение не должно вернуть
            # доступ к VPN. Отчёт остаётся на проверке — админ его отклонит.
            raise RewardTaskError(RewardTaskErrorCode.USER_BLOCKED)
        task = await db.get(RewardTask, task_id)
        if task is None:
            raise RewardTaskError(RewardTaskErrorCode.TASK_NOT_FOUND)
        if await _has_submission(db, task_id, user_id, RewardTaskSubmissionStatus.APPROVED):
            raise RewardTaskError(RewardTaskErrorCode.ALREADY_COMPLETED)
        if user.restriction_subscription:
            raise RewardTaskError(RewardTaskErrorCode.RESTRICTED)
        candidates = await _load_subscription_candidates(db, user_id, for_update=True)
        chosen = choose_reward_subscription(candidates)
        if chosen is None:
            # Подписку могли отключить, пока отчёт ждал проверки: subscription_disabled,
            # отчёт остаётся на проверке. DISABLED не оживляется (ответ владельца 2026-10-09).
            raise RewardTaskError(no_reward_subscription_reason(candidates))
        subscription = await _load_chosen_subscription(db, chosen, for_update=True)
        if subscription is None:
            raise RewardTaskError(RewardTaskErrorCode.NO_SUBSCRIPTION)
    except RewardTaskError:
        await _release(db)
        raise
    except Exception:
        logger.exception('Ошибка подготовки начисления за задание', task_id=task_id, user_id=user_id)
        raise await _fail(db, restore, RewardTaskErrorCode.INTERNAL_ERROR) from None

    # Всё, что понадобится после синхронизации, — простыми значениями: после
    # отката внутри неё ORM-объекты истекают.
    days = task.reward_days
    title = task.title
    subscription_id = subscription.id
    panel_ids = _panel_ids_for_update(subscription, user)

    submission.status = RewardTaskSubmissionStatus.APPROVED.value
    submission.reward_days = days
    submission.subscription_id = subscription_id
    submission.reviewed_by = reviewer_id
    submission.reviewed_at = datetime.now(UTC)
    if is_new:
        db.add(submission)
    try:
        await db.flush()
    except IntegrityError:
        # Мимо блокировки пользователя кто-то успел записать approved — награда уже есть.
        raise await _fail(db, restore, RewardTaskErrorCode.ALREADY_COMPLETED) from None
    except Exception:
        logger.exception('Не удалось сохранить отчёт как одобренный', task_id=task_id, user_id=user_id)
        raise await _fail(db, restore, RewardTaskErrorCode.INTERNAL_ERROR) from None
    submission_id = submission.id

    try:
        # Без tariff_id: is_trial не трогаем (бесплатные дни не делают триал платным).
        await extend_subscription(db, subscription, days, commit=False)
        end_date = subscription.end_date
        next_daily_charge_at = (
            _postpone_daily_charge(subscription, days, now=datetime.now(UTC)) if chosen.is_daily else None
        )
        # Сквады назначаются только при синхронизации созданием, но найти их нужно
        # сейчас: после её начала к ORM не обращаемся.
        missing_squads = await _resolve_missing_squads(db, subscription)
        await db.flush()
    except Exception:
        logger.exception(
            'Не удалось продлить подписку наградой за задание',
            submission_id=submission_id,
            subscription_id=subscription_id,
        )
        raise await _fail(db, restore, RewardTaskErrorCode.INTERNAL_ERROR) from None

    # С этого места к ORM-объектам не обращаемся: синхронизация могла откатить
    # сессию, и любое чтение поля упало бы с MissingGreenlet.
    synced = await _sync_panel(
        db, subscription, subscription_id=subscription_id, panel_ids=panel_ids, missing_squads=missing_squads
    )
    committed = False
    if synced:
        try:
            # Синхронизация уже закоммитила; явный commit — на случай, если её
            # контракт изменится (как в redeem_coupon).
            await db.commit()
            committed = True
        except Exception as error:
            logger.warning('Не удалось закоммитить начисление за задание', error=str(error))
    if not committed:
        await _safe_rollback(db)

    try:
        persisted = await _approved_in_db(db, submission_id) if submission_id is not None else False
    except Exception:
        logger.exception('Не удалось проверить итог начисления за задание', submission_id=submission_id)
        # После сбоя синхронизации награды почти наверняка нет, а повтор безопасен:
        # уже начисленное отобьют блокировка и уникальный индекс.
        code = RewardTaskErrorCode.INTERNAL_ERROR if synced else RewardTaskErrorCode.PANEL_SYNC_FAILED
        raise await _fail(db, restore, code) from None

    if not persisted:
        logger.warning(
            'Награда за задание не начислена: панель не приняла продление',
            submission_id=submission_id,
            task_id=task_id,
            user_id=user_id,
            subscription_id=subscription_id,
        )
        raise await _fail(db, restore, RewardTaskErrorCode.PANEL_SYNC_FAILED)

    if not committed:
        # Начисление закоммитила сама синхронизация, а сбой пришёл позже (например,
        # таймаут на закрытии клиента панели): награда есть, но откат после
        # коммита сделал объекты вызывающего истёкшими.
        logger.warning(
            'Награда за задание начислена, хотя синхронизация завершилась сбоем',
            submission_id=submission_id,
            subscription_id=subscription_id,
        )
        await _restore(db, restore)

    await _shift_lava_charge(db, subscription_id, days, restore)

    # У суточной ближайшее списание не позже end_date, у оживлённой — ровно в неё.
    daily = {'next_daily_charge_at': next_daily_charge_at} if next_daily_charge_at is not None else {}
    logger.info(
        '🎁 Начислена награда за задание',
        submission_id=submission_id,
        task_id=task_id,
        user_id=user_id,
        subscription_id=subscription_id,
        days=days,
        reviewer_id=reviewer_id,
        end_date=end_date,
        **daily,
    )
    result = RewardGrantResult(
        submission_id=submission_id,
        task_id=task_id,
        user_id=user_id,
        reward_days=days,
        subscription_id=subscription_id,
        end_date=end_date,
    )
    notice = _build_notice(
        user,
        NOTIFY_APPROVED_KEY,
        button=(NOTIFY_APPROVED_BUTTON_KEY, REWARD_TASKS_MENU_CALLBACK),
        reward_days=days,
        title=html.escape(title),
    )
    return result, notice


def _postpone_daily_charge(subscription: Subscription, days: int, *, now: datetime) -> datetime:
    """Переносит ближайшее суточное списание на ``days`` дней, но не дальше новой даты окончания.

    Возвращает, когда списание теперь будет. Вызывается после
    ``extend_subscription``: ``subscription.end_date`` — уже новая дата окончания.

    ``process_daily_charges`` списывает суточную подписку, когда с
    ``last_daily_charge_at`` прошли сутки, — дата окончания тут ни при чём. Без
    переноса оживлённую наградой подписку со старым списанием списали бы сразу
    после награды (без награды этого списания не было бы), а живой суточной
    подаренные дни почти ничего не давали бы. Принцип тот же, что у
    :func:`_shift_lava_charge`: награда сдвигает ближайшее рекуррентное
    списание. Ближайшее — через сутки после прошлого, но не раньше, чем сейчас
    (просроченное и «списаний ещё не было» — как «сейчас»), отсюда
    ``max(прошлое, now − 24 ч) + days``.

    И не позже новой даты окончания: ``last_daily_charge_at = min(max(прошлое,
    now − 24 ч) + days, новый конец − 24 ч)``. Без предела у истёкшей
    подписки с недавним списанием (срок кончился меньше чем через сутки после
    списания, ``last_daily_charge_at`` прежний) или со списанием, которое уже
    унесла вперёд прошлая награда, ближайшее списание встало бы на часы или дни
    позже нового конца. Пользователь панели в дату окончания
    выключается, а строка остаётся ACTIVE и не списывается: активные суточные
    без паузы monitoring не экспайрит. Для живых строк (обычных, с днями сверх
    суток от купона или админа, просроченных, после нескольких наград подряд)
    итог тот же, что без предела; у закончившихся, которые продлеваются от
    «сейчас», ближайшее списание приходится ровно на новую дату окончания. Как и у
    живых, которым админ сократил срок (окончание раньше прошлого списания + 24 ч):
    им предел тоже ставит списание ровно в новую дату окончания.

    Меняет только объект: вызывающий кладёт это в ту же транзакцию, что и
    продление, — оно коммитится и откатывается вместе с синхронизацией.
    """
    floor = now - DAILY_CHARGE_PERIOD
    last = _aware(subscription.last_daily_charge_at)
    base = last if last is not None and last > floor else floor
    latest = _aware(subscription.end_date) - DAILY_CHARGE_PERIOD
    postponed = min(base + timedelta(days=days), latest)
    subscription.last_daily_charge_at = postponed
    return postponed + DAILY_CHARGE_PERIOD


async def _shift_lava_charge(db: AsyncSession, subscription_id: int, days: int, restore: Sequence[Any]) -> None:
    """Сдвиг ближайшего автосписания Lava на подаренные дни (best-effort, после коммита).

    ``extend_subscription(commit=False)`` сам этого не делает: до коммита сдвиг
    на стороне Lava было бы нечем откатить.
    """
    try:
        from app.services.payment.lava import shift_lava_next_charge_after_manual_extension

        await shift_lava_next_charge_after_manual_extension(db, subscription_id, days)
    except Exception as error:
        logger.warning('Не удалось сдвинуть списание Lava после награды', subscription_id=subscription_id, error=error)
    try:
        # Хелпер только читал — закрываем его транзакцию.
        await db.commit()
    except Exception as error:
        logger.warning('Не удалось завершить транзакцию после сдвига Lava', error=str(error))
        await _safe_rollback(db)
        await _restore(db, restore)


# ── Уведомления ──────────────────────────────────────────────────────────────


def _build_notice(
    user: _UserSnapshot | Any,
    key: str,
    *,
    button: tuple[str, str] | None = None,
    reward_days: int | None = None,
    **values: Any,
) -> _Notice | None:
    """Текст уведомления на языке пользователя; None — если слать некуда.

    ``button`` — (ключ локали с текстом кнопки, callback_data) под сообщением.
    ``reward_days`` подставляется как ``{reward}`` в той же форме, что на экранах
    бота («+1 day» / «+3 days»), и как ``{days}`` — для переопределённых шаблонов.
    """
    telegram_id = getattr(user, 'telegram_id', None)
    if not telegram_id:
        return None
    if getattr(user, 'status', None) in (UserStatus.BLOCKED.value, UserStatus.DELETED.value):
        return None
    texts = get_texts(getattr(user, 'language', None))
    template = texts.get(key)
    if not template:
        return None
    if reward_days is not None:
        values = {'reward': format_reward_days(texts, reward_days), 'days': reward_days, **values}

    reply_markup = None
    if button is not None:
        button_key, callback_data = button
        label = texts.get(button_key)
        if label:
            # Текст кнопки Telegram не парсит как HTML: <tg-emoji> из локалей показался бы тегом.
            reply_markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=strip_tg_emoji_tags(label), callback_data=callback_data)]]
            )
    return _Notice(
        telegram_id=telegram_id,
        text=template.format_map(_FormatValues(values)),
        reply_markup=reply_markup,
    )


async def _send_notice(bot: Any, notice: _Notice, semaphore: asyncio.Semaphore) -> None:
    kwargs: dict[str, Any] = {'chat_id': notice.telegram_id, 'text': notice.text, 'parse_mode': 'HTML'}
    if notice.reply_markup is not None:
        kwargs['reply_markup'] = notice.reply_markup
    async with semaphore:
        try:
            await asyncio.wait_for(bot.send_message(**kwargs), timeout=NOTIFY_TIMEOUT_SECONDS)
        except Exception as error:
            logger.warning(
                'Не удалось отправить уведомление по заданию',
                telegram_id=notice.telegram_id,
                error=str(error),
            )


async def _deliver_notices(notices: Iterable[_Notice | None], bot: Any = None) -> None:
    """Шлёт уведомления через бота, не больше ``NOTIFY_CONCURRENCY`` разом.

    Best-effort: ошибка одного сообщения не мешает остальным и только логируется.
    """
    pending = [notice for notice in notices if notice is not None]
    if not pending:
        return
    own_bot = None
    try:
        if bot is None:
            own_bot = bot = create_bot()
        semaphore = asyncio.Semaphore(NOTIFY_CONCURRENCY)
        await asyncio.gather(*(_send_notice(bot, notice, semaphore) for notice in pending))
    except Exception as error:
        logger.warning('Уведомления по заданиям не отправлены', error=str(error))
    finally:
        if own_bot is not None:
            try:
                await own_bot.session.close()
            except Exception:
                pass


# ── Проверка отчётов админом ─────────────────────────────────────────────────


def _unique_ids(ids: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for raw in ids:
        value = int(raw)
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


async def approve(
    db: AsyncSession,
    ids: Iterable[int],
    reviewer: User | None,
    *,
    bot: Any = None,
) -> list[ReviewResult]:
    """Одобряет отчёты: каждый id — своя транзакция и свой результат.

    Одобрить можно только pending; отчёт заблокированного пользователя не
    одобряется (``user_blocked``) и остаётся на проверке. Уведомления уходят
    после обработки всех id. ``reviewer`` после сбоя начисления перечитывается —
    им можно пользоваться дальше.
    """
    reviewer_id = reviewer.id if reviewer is not None else None
    restore = (reviewer,) if reviewer is not None else ()
    results: list[ReviewResult] = []
    notices: list[tuple[int, _Notice | None]] = []  # (автор, уведомление)

    for submission_id in _unique_ids(ids):
        try:
            submission = await db.get(RewardTaskSubmission, submission_id)
            if submission is None:
                raise RewardTaskError(RewardTaskErrorCode.NOT_FOUND)
            if submission.status != RewardTaskSubmissionStatus.PENDING:
                raise RewardTaskError(RewardTaskErrorCode.NOT_PENDING)
            grant_result, notice = await _grant(db, submission, reviewer_id=reviewer_id, restore=restore)
        except RewardTaskError as error:
            results.append(ReviewResult(id=submission_id, ok=False, error_code=error.code))
            continue
        except Exception:
            logger.exception('Ошибка одобрения отчёта по заданию', submission_id=submission_id)
            await _safe_rollback(db)
            await _restore(db, restore)
            results.append(
                ReviewResult(id=submission_id, ok=False, error_code=RewardTaskErrorCode.INTERNAL_ERROR.value)
            )
            continue
        results.append(ReviewResult(id=submission_id, ok=True))
        notices.append((grant_result.user_id, notice))

    # Дни начислены, но раздел автору сейчас скрыт (режим «только админы»): кнопка
    # «🎁 Задания» ответила бы «Раздел недоступен» — шлём уведомление без неё.
    hidden = await _authors_hidden_from_tasks(db, [user_id for user_id, notice in notices if notice is not None])
    for user_id, notice in notices:
        if notice is not None and user_id in hidden:
            notice.reply_markup = None
    await _deliver_notices([notice for _user_id, notice in notices], bot=bot)
    return results


def normalize_reject_reason(reason: str | None) -> str:
    text = (reason or '').strip()
    if not text or len(text) > MAX_REJECT_REASON_LENGTH:
        raise RewardTaskError(RewardTaskErrorCode.INVALID_REASON, max=MAX_REJECT_REASON_LENGTH)
    return text


async def reject(
    db: AsyncSession,
    ids: Iterable[int],
    reviewer: User | None,
    reason: str,
    *,
    bot: Any = None,
) -> list[ReviewResult]:
    """Отклоняет отчёты с причиной (её видит пользователь); после — можно прислать новый."""
    text = normalize_reject_reason(reason)
    reviewer_id = reviewer.id if reviewer is not None else None
    results: list[ReviewResult] = []
    rejected: list[tuple[int, int]] = []  # (user_id, task_id)

    for submission_id in _unique_ids(ids):
        try:
            submission = await _lock_submission(db, submission_id)
            if submission is None:
                await _release(db)
                results.append(ReviewResult(id=submission_id, ok=False, error_code=RewardTaskErrorCode.NOT_FOUND.value))
                continue
            if submission.status != RewardTaskSubmissionStatus.PENDING:
                await _release(db)
                results.append(
                    ReviewResult(id=submission_id, ok=False, error_code=RewardTaskErrorCode.NOT_PENDING.value)
                )
                continue
            submission.status = RewardTaskSubmissionStatus.REJECTED.value
            submission.reject_reason = text
            submission.reviewed_by = reviewer_id
            submission.reviewed_at = datetime.now(UTC)
            rejected_pair = (submission.user_id, submission.task_id)
            await db.commit()
        except Exception:
            logger.exception('Ошибка отклонения отчёта по заданию', submission_id=submission_id)
            await _safe_rollback(db)
            results.append(
                ReviewResult(id=submission_id, ok=False, error_code=RewardTaskErrorCode.INTERNAL_ERROR.value)
            )
            continue
        results.append(ReviewResult(id=submission_id, ok=True))
        rejected.append(rejected_pair)
        logger.info('❌ Отчёт по заданию отклонён', submission_id=submission_id, reviewer_id=reviewer_id)

    if rejected:
        try:
            notices = await _rejection_notices(db, rejected, text)
        except Exception as error:
            logger.warning('Не удалось подготовить уведомления об отклонении', error=str(error))
            notices = []
        await _deliver_notices(notices, bot=bot)
    return results


async def _rejection_notices(
    db: AsyncSession, rejected: Sequence[tuple[int, int]], reason: str
) -> list[_Notice | None]:
    user_ids = sorted({user_id for user_id, _ in rejected})
    task_ids = sorted({task_id for _, task_id in rejected})
    users_result = await db.execute(
        select(User.id, User.telegram_id, User.language, User.status).where(User.id.in_(user_ids))
    )
    users = {row.id: row for row in users_result.all()}
    tasks_result = await db.execute(select(RewardTask.id, RewardTask.title).where(RewardTask.id.in_(task_ids)))
    titles = {row.id: row.title for row in tasks_result.all()}
    hidden = await _authors_hidden_from_tasks(db, user_ids)

    notices: list[_Notice | None] = []
    for user_id, task_id in rejected:
        user = users.get(user_id)
        if user is None:
            continue
        if user_id in hidden:
            # Раздел автору скрыт (отключённая подписка, режим «только админы»):
            # прислать отчёт заново он не сможет — без приглашения и без кнопки.
            key, button = NOTIFY_REJECTED_PLAIN_KEY, None
        else:
            # Кнопка ведёт на экран задания: там «📸 Отправить новый отчёт».
            key, button = NOTIFY_REJECTED_KEY, (NOTIFY_REJECTED_BUTTON_KEY, f'{TASK_CALLBACK_PREFIX}{task_id}')
        notices.append(
            _build_notice(
                user,
                key,
                button=button,
                title=html.escape(titles.get(task_id, '')),
                reason=html.escape(reason),
            )
        )
    return notices


async def _authors_hidden_from_tasks(db: AsyncSession, user_ids: Iterable[int]) -> set[int]:
    """Авторы отчётов, которым раздел «Задания» сейчас не виден (:func:`get_reward_tasks_access`).

    Уведомление о проверке им уходит без кнопки в раздел и без приглашения прислать
    отчёт заново: кнопка ответила бы «Раздел недоступен». Поля пользователя —
    отдельным запросом колонок, а не ``User`` из identity map: его мог загрузить
    вызывающий, и перечитывание сбросило бы ему связи. Сбой проверки — уведомление
    как раньше: оно важнее кнопки.
    """
    ids = sorted(set(user_ids))
    if not ids:
        return set()
    rows = await db.execute(
        select(
            User.id,
            User.telegram_id,
            User.status,
            User.email,
            User.email_verified,
            User.email_verification_source,
            User.restriction_subscription,
        ).where(User.id.in_(ids))
    )
    hidden: set[int] = set()
    for row in rows.all():
        author = SimpleNamespace(**{key: row._mapping[key] for key in row._mapping.keys()})
        try:
            access = await get_reward_tasks_access(db, author)
        except Exception as error:
            logger.warning('Не удалось проверить, виден ли автору раздел заданий', user_id=row.id, error=str(error))
            continue
        if not access.visible:
            hidden.add(row.id)
    return hidden


# ── Управление заданиями (админка) ───────────────────────────────────────────

_TASK_KINDS = frozenset(kind.value for kind in RewardTaskKind)
_TASK_FIELDS = (
    'kind',
    'title',
    'description',
    'reward_days',
    'action_url',
    'channel_id',
    'min_boosts',
    'max_photos',
    'is_active',
    'sort_order',
)


def _invalid(field: str, reason: str) -> RewardTaskError:
    return RewardTaskError(RewardTaskErrorCode.INVALID_TASK, field=field, reason=reason)


def normalize_action_url(value: str | None) -> str | None:
    """Ссылка кнопки задания: пусто → None; иначе только http(s) без пробелов.

    Чужие схемы (``javascript:``, ``data:``) не пропускаем: кабинет рисует ссылку
    как есть, а Telegram отвергает всё сообщение из-за кривой URL-кнопки.
    """
    text = (value or '').strip()
    if not text:
        return None
    if len(text) > ACTION_URL_MAX_LENGTH:
        raise ValueError(f'action_url is longer than {ACTION_URL_MAX_LENGTH} characters')
    parsed = urlparse(text)
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.netloc or any(ch.isspace() for ch in text):
        raise ValueError('action_url must be an http(s) link')
    return text


def normalize_channel_id(value: str | None) -> str:
    """Канал для проверки буста: ``-100…`` (голые цифры дополняются) или ``@username``."""
    text = (value or '').strip()
    if not text:
        raise ValueError('channel_id is required for channel_boost tasks')
    if len(text) > CHANNEL_ID_MAX_LENGTH:
        raise ValueError(f'channel_id is longer than {CHANNEL_ID_MAX_LENGTH} characters')
    if text.startswith('@'):
        if not _CHANNEL_USERNAME_RE.match(text):
            raise ValueError('channel_id must be a numeric channel id or @username')
        return text
    return validate_channel_id(text)


def _int_in_range(data: Mapping[str, Any], field: str, low: int, high: int) -> int:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(field, 'must be an integer')
    if not low <= value <= high:
        raise _invalid(field, f'must be between {low} and {high}')
    return value


def validate_task_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    """Проверяет и нормализует ПОЛНОЕ состояние задания (создание или итог правки).

    Бросает ``RewardTaskError('invalid_task', field=..., reason=...)``.
    """
    merged = {
        'description': '',
        'action_url': None,
        'channel_id': None,
        'min_boosts': DEFAULT_MIN_BOOSTS,
        'max_photos': DEFAULT_MAX_PHOTOS,
        'is_active': True,
        'sort_order': 0,
        **{key: value for key, value in data.items() if key in _TASK_FIELDS},
    }

    kind = merged.get('kind')
    if kind not in _TASK_KINDS:
        raise _invalid('kind', f'must be one of {sorted(_TASK_KINDS)}')

    title = merged.get('title')
    title = title.strip() if isinstance(title, str) else ''
    if not title or len(title) > TITLE_MAX_LENGTH:
        raise _invalid('title', f'must be 1..{TITLE_MAX_LENGTH} characters')

    description = merged.get('description') or ''
    if not isinstance(description, str):
        raise _invalid('description', 'must be a string')
    description = description.strip()
    if len(description) > DESCRIPTION_MAX_LENGTH:
        raise _invalid('description', f'must be at most {DESCRIPTION_MAX_LENGTH} characters')

    try:
        action_url = normalize_action_url(merged.get('action_url'))
    except ValueError as error:
        raise _invalid('action_url', str(error)) from None

    channel_id = None
    if kind in (RewardTaskKind.CHANNEL_BOOST, RewardTaskKind.CHANNEL_SUBSCRIPTION):
        try:
            channel_id = normalize_channel_id(merged.get('channel_id'))
        except ValueError as error:
            raise _invalid('channel_id', str(error)) from None

    is_active = merged.get('is_active')
    if not isinstance(is_active, bool):
        raise _invalid('is_active', 'must be a boolean')

    return {
        'kind': str(kind),
        'title': title,
        'description': description,
        'reward_days': _int_in_range(merged, 'reward_days', REWARD_DAYS_MIN, REWARD_DAYS_MAX),
        'action_url': action_url,
        'channel_id': channel_id,
        'min_boosts': _int_in_range(merged, 'min_boosts', 1, MIN_BOOSTS_MAX),
        'max_photos': _int_in_range(merged, 'max_photos', 1, MAX_PHOTOS_LIMIT),
        'is_active': is_active,
        'sort_order': _int_in_range(merged, 'sort_order', -SORT_ORDER_ABS_MAX, SORT_ORDER_ABS_MAX),
    }


def task_fields(task: RewardTask) -> dict[str, Any]:
    """Текущие значения редактируемых полей (база для слияния при PATCH)."""
    return {field: getattr(task, field) for field in _TASK_FIELDS}


async def task_has_submissions(db: AsyncSession, task_id: int) -> bool:
    result = await db.execute(select(RewardTaskSubmission.id).where(RewardTaskSubmission.task_id == task_id).limit(1))
    return result.scalar_one_or_none() is not None


async def list_tasks_with_counts(db: AsyncSession, task_ids: Sequence[int] | None = None) -> list[TaskWithCounts]:
    """Задания (включая выключенные) с числом отчётов на проверке и одобренных.

    ``task_ids`` — только эти задания (например, одно после правки).
    """
    tasks_query = select(RewardTask).order_by(RewardTask.sort_order, RewardTask.id)
    counts_query = (
        select(RewardTaskSubmission.task_id, RewardTaskSubmission.status, func.count(RewardTaskSubmission.id))
        .where(
            RewardTaskSubmission.status.in_(
                [RewardTaskSubmissionStatus.PENDING.value, RewardTaskSubmissionStatus.APPROVED.value]
            )
        )
        .group_by(RewardTaskSubmission.task_id, RewardTaskSubmission.status)
    )
    if task_ids is not None:
        tasks_query = tasks_query.where(RewardTask.id.in_(list(task_ids)))
        counts_query = counts_query.where(RewardTaskSubmission.task_id.in_(list(task_ids)))
    tasks = list((await db.execute(tasks_query)).scalars().all())
    counts_result = await db.execute(counts_query)
    counts: dict[tuple[int, str], int] = {(task_id, status): count for task_id, status, count in counts_result.all()}
    return [
        TaskWithCounts(
            task=task,
            pending_count=counts.get((task.id, RewardTaskSubmissionStatus.PENDING.value), 0),
            approved_count=counts.get((task.id, RewardTaskSubmissionStatus.APPROVED.value), 0),
        )
        for task in tasks
    ]


async def create_task(db: AsyncSession, data: Mapping[str, Any]) -> RewardTask:
    fields = validate_task_fields(data)
    now = datetime.now(UTC)
    task = RewardTask(**fields, created_at=now, updated_at=now)
    db.add(task)
    await db.commit()
    invalidate_active_tasks_cache()
    logger.info('Создано задание', task_id=task.id, kind=task.kind, reward_days=task.reward_days)
    return task


async def update_task(db: AsyncSession, task: RewardTask, changes: Mapping[str, Any]) -> RewardTask:
    """Правка задания: проверяется итоговое состояние, а не кусок запроса.

    Вид задания с отчётами менять нельзя — pending-фотоотчёт стал бы «бустом».
    """
    fields = validate_task_fields({**task_fields(task), **changes})
    if fields['kind'] != task.kind and await task_has_submissions(db, task.id):
        raise RewardTaskError(RewardTaskErrorCode.TASK_HAS_SUBMISSIONS)
    for key, value in fields.items():
        setattr(task, key, value)
    # Явное значение вместо onupdate=now(): иначе поле истечёт после UPDATE и
    # чтение в async-ответе упадёт с MissingGreenlet.
    task.updated_at = datetime.now(UTC)
    await db.commit()
    invalidate_active_tasks_cache()
    logger.info('Задание изменено', task_id=task.id)
    return task


async def delete_task(db: AsyncSession, task: RewardTask) -> None:
    """Удаляет задание без отчётов; с отчётами — только выключить (история наград)."""
    if await task_has_submissions(db, task.id):
        raise RewardTaskError(RewardTaskErrorCode.TASK_HAS_SUBMISSIONS)
    task_id = task.id
    await db.delete(task)
    await db.commit()
    invalidate_active_tasks_cache()
    logger.info('Задание удалено', task_id=task_id)


# ── Отчёты для админки ───────────────────────────────────────────────────────


async def list_submissions(
    db: AsyncSession,
    *,
    status: str | None = None,
    task_id: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[RewardTaskSubmission], int, int]:
    """Отчёты для админки: ``(items, total, pending_count)``.

    На проверке — самые старые первыми (очередь), остальные — последние
    обработанные первыми. ``pending_count`` — по всем заданиям, для бейджа вкладки.
    """
    filters = []
    if status is not None:
        filters.append(RewardTaskSubmission.status == status)
    if task_id is not None:
        filters.append(RewardTaskSubmission.task_id == task_id)

    if status == RewardTaskSubmissionStatus.PENDING:
        order = (RewardTaskSubmission.created_at.asc(), RewardTaskSubmission.id.asc())
    else:
        order = (
            func.coalesce(RewardTaskSubmission.reviewed_at, RewardTaskSubmission.created_at).desc(),
            RewardTaskSubmission.id.desc(),
        )

    items_result = await db.execute(
        select(RewardTaskSubmission)
        .options(
            selectinload(RewardTaskSubmission.task),
            selectinload(RewardTaskSubmission.user),
            selectinload(RewardTaskSubmission.reviewer),
        )
        .where(*filters)
        .order_by(*order)
        .offset(offset)
        .limit(limit)
    )
    items = list(items_result.scalars().all())

    total_result = await db.execute(select(func.count(RewardTaskSubmission.id)).where(*filters))
    pending_result = await db.execute(
        select(func.count(RewardTaskSubmission.id)).where(
            RewardTaskSubmission.status == RewardTaskSubmissionStatus.PENDING.value
        )
    )
    return items, int(total_result.scalar() or 0), int(pending_result.scalar() or 0)
