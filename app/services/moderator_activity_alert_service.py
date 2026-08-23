"""Алерт на массовый просмотр базы клиентов модератором.

Роль «Поддержка» имеет ``users:read`` — это нужно для работы с тикетами, но
той же ручкой можно листать всю базу. Отличить работу от выгрузки помогает
объём: саппорт открывает единицы карточек (те, что в его тикетах), выгрузка —
десятки карточек или страниц листинга подряд.

Сервис считает по ``admin_audit_log`` за скользящее окно и, если модератор
перешагнул порог, шлёт разовое (с кулдауном) уведомление в админ-чат.
Суперадминов не трогаем — им база положена по должности.
"""

import re
import time
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import AdminAuditLog, User


logger = structlog.get_logger(__name__)

USERS_READ_PERMISSION = 'users:read'

# /cabinet/admin/users/12057, /cabinet/admin/users/12057/referrals — карточка конкретного клиента.
_PROFILE_PATH_RE = re.compile(r'/cabinet/admin/users/(\d+)')
# /cabinet/admin/users — листинг базы; глубину даёт offset+limit из query_params.
_LIST_PATH = '/cabinet/admin/users'
_DEFAULT_LIST_LIMIT = 20

# Больше строк за окно один человек руками не откроет — читаем с запасом и не грузим БД.
_MAX_ROWS_PER_SCAN = 3000

# Кулдаун на случай, если Redis недоступен: процесс кабинета один, словаря хватает.
_local_cooldown: dict[int, float] = {}


def _cooldown_seconds() -> int:
    return max(1, settings.MODERATOR_SCRAPE_ALERT_COOLDOWN_MINUTES) * 60


async def _claim_alert_slot(user_id: int) -> bool:
    """True — алерт по этому модератору сейчас можно слать (кулдаун не активен)."""
    from app.utils.cache import cache

    key = f'moderator_scrape_alert:{user_id}'
    if getattr(cache, '_connected', False):
        return await cache.setnx(key, datetime.now(UTC).isoformat(), expire=_cooldown_seconds())

    now = time.monotonic()
    last = _local_cooldown.get(user_id)
    if last is not None and now - last < _cooldown_seconds():
        return False
    _local_cooldown[user_id] = now
    return True


async def _is_superadmin(db: AsyncSession, user_id: int) -> bool:
    from app.database.crud.rbac import SUPERADMIN_LEVEL, UserRoleCRUD

    _permissions, _role_names, max_level = await UserRoleCRUD.get_user_permissions(db, user_id)
    return max_level >= SUPERADMIN_LEVEL


async def _collect_events(db: AsyncSession, user_id: int, since: datetime) -> list[tuple[str, dict]]:
    result = await db.execute(
        select(AdminAuditLog.request_path, AdminAuditLog.details)
        .where(
            AdminAuditLog.user_id == user_id,
            AdminAuditLog.status == 'success',
            AdminAuditLog.action.like(f'%{USERS_READ_PERMISSION}%'),
            AdminAuditLog.created_at >= since,
        )
        .order_by(AdminAuditLog.id.desc())
        .limit(_MAX_ROWS_PER_SCAN)
    )
    return [(path, details or {}) for path, details in result.all() if path]


def _measure(events: list[tuple[str, dict]]) -> tuple[set[str], int]:
    """Возвращает (id открытых карточек, глубину листания базы в строках).

    Глубина — максимальный ``offset + limit`` среди запросов листинга БЕЗ поиска.
    Поиск по имени/id — обычная работа саппорта (запрос на каждое нажатие клавиши),
    а вот проход по offset 0→20→40… или разовый ``limit=200`` и есть выкачивание.
    """
    profiles: set[str] = set()
    reach = 0
    for path, details in events:
        match = _PROFILE_PATH_RE.match(path)
        if match:
            profiles.add(match.group(1))
            continue
        if path != _LIST_PATH:
            continue
        params = details.get('query_params') or {}
        if params.get('search'):
            continue
        try:
            offset = int(params.get('offset') or 0)
            limit = int(params.get('limit') or _DEFAULT_LIST_LIMIT)
        except (TypeError, ValueError):
            offset, limit = 0, _DEFAULT_LIST_LIMIT
        reach = max(reach, offset + limit)
    return profiles, reach


def _build_message(
    *,
    user: User,
    profiles: set[str],
    reach: int,
    window_minutes: int,
    client_ip: str | None,
) -> str:
    from app.utils.timezone import format_local_datetime

    username = f'@{user.username}' if user.username else '—'
    lines = [
        '🚨 <b>ПОДОЗРЕНИЕ НА ВЫГРУЗКУ БАЗЫ</b>',
        '',
        f'👮 <b>Модератор:</b> {username} (id {user.id}, tg {user.telegram_id})',
        f'<tg-emoji emoji-id="5262698112975125264">⏰</tg-emoji> <b>Окно:</b> последние {window_minutes} мин',
        '',
        f'👥 Открыто карточек клиентов: <b>{len(profiles)}</b>'
        f' (порог {settings.MODERATOR_SCRAPE_ALERT_PROFILES})',
        f'📄 Пролистано базы: <b>{reach}</b> строк'
        f' (порог {settings.MODERATOR_SCRAPE_ALERT_LIST_ROWS})',
    ]
    if client_ip:
        lines.append(f'🌐 IP последнего запроса: <code>{client_ip}</code>')
    lines.extend(
        [
            '',
            'Проверьте журнал: <code>/cabinet/admin/audit-log</code> — фильтр по этому пользователю.',
            f'<i>{format_local_datetime(datetime.now(UTC), "%d.%m.%Y %H:%M:%S")}</i>',
        ]
    )
    return '\n'.join(lines)


async def _send_alert(text: str) -> None:
    from app.bot_factory import create_bot
    from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

    bot = create_bot()
    try:
        service = AdminNotificationService(bot)
        await service.send_admin_notification(text, category=NotificationCategory.PARTNERS)
    finally:
        await bot.session.close()


async def check_users_read_activity(db: AsyncSession, user: User, action: str) -> None:
    """Вызывается после успешной записи в аудит; сама решает, молчать или бить тревогу.

    Никогда не бросает наружу: алерт не должен ломать обычный запрос модератора.
    """
    try:
        if not getattr(settings, 'MODERATOR_SCRAPE_ALERT_ENABLED', False):
            return
        if USERS_READ_PERMISSION not in action:
            return
        if not settings.is_admin_notifications_enabled():
            return

        window_minutes = max(1, settings.MODERATOR_SCRAPE_ALERT_WINDOW_MINUTES)
        since = datetime.now(UTC) - timedelta(minutes=window_minutes)

        events = await _collect_events(db, user.id, since)
        profiles, reach = _measure(events)

        over_profiles = len(profiles) >= settings.MODERATOR_SCRAPE_ALERT_PROFILES
        over_reach = reach >= settings.MODERATOR_SCRAPE_ALERT_LIST_ROWS
        if not (over_profiles or over_reach):
            return

        if await _is_superadmin(db, user.id):
            return

        if not await _claim_alert_slot(user.id):
            logger.debug('Алерт по выгрузке базы подавлен кулдауном', user_id=user.id)
            return

        last_ip = await db.execute(
            select(AdminAuditLog.ip_address)
            .where(AdminAuditLog.user_id == user.id)
            .order_by(AdminAuditLog.id.desc())
            .limit(1)
        )
        client_ip = last_ip.scalar_one_or_none()

        logger.warning(
            'Модератор перешагнул порог просмотра базы',
            user_id=user.id,
            profiles=len(profiles),
            reach=reach,
            window_minutes=window_minutes,
        )
        await _send_alert(
            _build_message(
                user=user,
                profiles=profiles,
                reach=reach,
                window_minutes=window_minutes,
                client_ip=client_ip,
            )
        )
    except Exception as error:
        logger.warning('Не удалось проверить активность модератора', user_id=user.id, error=str(error))
        # Сессию дальше использует сам маршрут — оставлять её в сломанной транзакции нельзя.
        try:
            await db.rollback()
        except Exception:
            pass
