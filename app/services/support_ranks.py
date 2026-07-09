"""
Ранги модераторов поддержки (перенесены из бота LEDOKOL TALK).

Ранг присваивается по количеству закрытых тикетов (нарастающим итогом):
счётчик = закрытые тикеты в этом боте + офсет из старого бота
(support_settings.json → rank_offsets, ключ — telegram_id).

Ранги видны только в панели модераторов и рейтинге — пользователям
в ответах поддержки ранг не показывается.

Premium custom emoji: Telegram показывает их как анимированные иконки только
пользователям с Telegram Premium, у остальных отображается fallback-эмодзи.
Формат: <tg-emoji emoji-id="...">fallback</tg-emoji> при parse_mode="HTML".
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.ticket import TicketCRUD
from app.database.models import User


# (порог тикетов, custom_emoji_id premium-эмодзи, fallback-эмодзи, название)
RANKS: list[tuple[int, str, str, str]] = [
    (2, '5346025525104090361', '👨‍💻', 'Членосос'),
    (200, '5348041191910808740', '👨‍💻', 'Рыцарь'),
    (400, '5345910471520165551', '👨‍💻', 'Майор'),
    (600, '5345843878052240650', '🪨', 'La Generale'),
    (800, '5346106270489256950', '🪨', 'Диктатор'),
    (1000, '5348513161572000418', '🪨', 'Гачи бог'),
    (1200, '5348422649431203798', '🪨', 'Владыка морей'),
    (1400, '5348548955829443889', '🪨', 'Заглот'),
    (1600, '5222235642691340539', '👊', 'Магистр осеменитель'),
    (1800, '5222085739742773259', '👊', 'Верховный пидор'),
    (2000, '5224312401112873835', '👊', 'La Bimba (доктор пидор)'),
    (2200, '5222273953799623317', '👊', 'Penis Le Grande'),
    (2400, '5224274231738515290', '👊', 'Супершишка'),
    (2600, '5222010745318819818', '👊', 'Порванное очко'),
    (2800, '5222243949158092463', '👊', 'Бог Спермобак'),
]

DEFAULT_RANK: tuple[str, str, str] = ('5408936440824353661', '👻', 'Дух')


def get_rank(closed_tickets: int) -> tuple[str, str, str]:
    """Возвращает (emoji_id, fallback_icon, название) текущего ранга."""
    current = DEFAULT_RANK
    for threshold, emoji_id, icon, name in RANKS:
        if closed_tickets >= threshold:
            current = (emoji_id, icon, name)
        else:
            break
    return current


def rank_emoji_html(emoji_id: str, fallback_icon: str) -> str:
    if not emoji_id:
        return fallback_icon
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback_icon}</tg-emoji>'


def get_rank_label(closed_tickets: int) -> str:
    """'<tg-emoji ...>👊</tg-emoji> Название' — HTML-метка ранга с премиум-иконкой."""
    emoji_id, icon, name = get_rank(closed_tickets)
    return f'{rank_emoji_html(emoji_id, icon)} {name}'


def get_rank_name(closed_tickets: int) -> str:
    """Название ранга без HTML (для кабинета/API)."""
    return get_rank(closed_tickets)[2]


def get_next_rank(closed_tickets: int) -> tuple[int, str, str, str] | None:
    for threshold, emoji_id, icon, name in RANKS:
        if closed_tickets < threshold:
            return threshold, emoji_id, icon, name
    return None


def progress_text(closed_tickets: int) -> str:
    """Строка вида '👻 Дух • ещё 2 тикет(ов) до 👨‍💻 Членосос' (HTML)."""
    label = get_rank_label(closed_tickets)
    nxt = get_next_rank(closed_tickets)
    if nxt is None:
        return f'{label} (максимальный ранг)'
    threshold, emoji_id, icon, name = nxt
    remaining = threshold - closed_tickets
    next_emoji = rank_emoji_html(emoji_id, icon)
    return f'{label} • ещё {remaining} тикет(ов) до {next_emoji} {name}'


def _get_offsets() -> dict[int, int]:
    from app.services.support_settings_service import SupportSettingsService

    return SupportSettingsService.get_rank_offsets()


async def get_closed_count(db: AsyncSession, user: User) -> int:
    """Полный счётчик закрытых тикетов модератора: этот бот + офсет из старого."""
    count = await TicketCRUD.get_closed_count_for_user(db, user.id)
    offsets = _get_offsets()
    telegram_id = getattr(user, 'telegram_id', None)
    if telegram_id:
        count += int(offsets.get(int(telegram_id), 0))
    return count


async def get_leaderboard(db: AsyncSession, limit: int = 15) -> list[dict]:
    """Рейтинг модераторов: [{telegram_id, user_id, display, username, closed}], по убыванию."""
    offsets = dict(_get_offsets())
    rows = await TicketCRUD.get_closed_counts_by_moderator(db)

    leaderboard: dict[int, dict] = {}
    for user, closed in rows:
        telegram_id = int(user.telegram_id) if getattr(user, 'telegram_id', None) else None
        total = closed + (offsets.pop(telegram_id, 0) if telegram_id else 0)
        key = telegram_id or -user.id
        leaderboard[key] = {
            'telegram_id': telegram_id,
            'user_id': user.id,
            'display': (user.full_name or user.username or (str(telegram_id) if telegram_id else f'#{user.id}')),
            'username': user.username,
            'closed': total,
        }

    # Модераторы, у которых есть только офсет из старого бота — имена берём из users, если есть
    remaining = {tid: closed for tid, closed in offsets.items() if closed > 0}
    if remaining:
        from sqlalchemy import select

        result = await db.execute(select(User).where(User.telegram_id.in_(list(remaining.keys()))))
        users_by_tid = {int(u.telegram_id): u for u in result.scalars().all() if u.telegram_id}
        for telegram_id, closed in remaining.items():
            user = users_by_tid.get(telegram_id)
            leaderboard[telegram_id] = {
                'telegram_id': telegram_id,
                'user_id': user.id if user else None,
                'display': (user.full_name or user.username) if user else str(telegram_id),
                'username': user.username if user else None,
                'closed': int(closed),
            }

    result = sorted(leaderboard.values(), key=lambda item: item['closed'], reverse=True)
    return result[:limit]
