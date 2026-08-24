"""CRUD для пожертвований «Поддержать проект».

Отдельный от ``transactions`` реестр: переживает удаление аккаунта донора
(снимок донора денормализован, FK ``SET NULL``), поэтому статистика «топ
спонсоров» не забывает ушедших пользователей.
"""

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Select

from app.database.models import Donation, User


logger = structlog.get_logger(__name__)


def _donor_snapshot(user: User | None) -> dict[str, Any]:
    """Собирает снимок донора из объекта пользователя (best-effort)."""
    if user is None:
        return {}
    first_name = getattr(user, 'first_name', None) or ''
    last_name = getattr(user, 'last_name', None) or ''
    full_name = (f'{first_name} {last_name}').strip() or None
    return {
        'donor_telegram_id': getattr(user, 'telegram_id', None),
        'donor_username': getattr(user, 'username', None),
        'donor_name': full_name,
    }


async def donation_exists(
    db: AsyncSession,
    *,
    external_id: str | None,
    payment_method: str | None = None,
) -> bool:
    """Идемпотентность: есть ли уже пожертвование с таким (external_id, payment_method)."""
    if not external_id:
        return False
    query = select(Donation.id).where(Donation.external_id == str(external_id))
    if payment_method is not None:
        query = query.where(Donation.payment_method == payment_method)
    result = await db.execute(query.limit(1))
    return result.scalars().first() is not None


async def create_donation(
    db: AsyncSession,
    *,
    user: User | None = None,
    user_id: int | None = None,
    amount_kopeks: int,
    payment_method: str | None = None,
    external_id: str | None = None,
    transaction_id: int | None = None,
    donor_telegram_id: int | None = None,
    donor_username: str | None = None,
    donor_name: str | None = None,
    source: str = 'menu',
    commit: bool = True,
) -> Donation:
    """Создаёт запись пожертвования. При переданном ``user`` сам заполняет снимок донора."""
    snapshot = _donor_snapshot(user)

    donation = Donation(
        user_id=user_id if user_id is not None else getattr(user, 'id', None),
        donor_telegram_id=donor_telegram_id if donor_telegram_id is not None else snapshot.get('donor_telegram_id'),
        donor_username=donor_username if donor_username is not None else snapshot.get('donor_username'),
        donor_name=donor_name if donor_name is not None else snapshot.get('donor_name'),
        amount_kopeks=amount_kopeks,
        payment_method=payment_method,
        external_id=str(external_id) if external_id is not None else None,
        transaction_id=transaction_id,
        source=source,
    )

    db.add(donation)
    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(donation)

    logger.info(
        '❤️ Создана запись пожертвования',
        donation_id=donation.id,
        amount_kopeks=amount_kopeks,
        user_id=donation.user_id,
        payment_method=payment_method,
    )
    return donation


def _apply_period(query: Select, start: datetime | None, end: datetime | None) -> Select:
    """Опционально ограничивает выборку пожертвований периодом по created_at."""
    if start is not None:
        query = query.where(Donation.created_at >= start)
    if end is not None:
        query = query.where(Donation.created_at <= end)
    return query


async def get_total_donations_kopeks(
    db: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Общая сумма всех пожертвований в копейках (опц. за период)."""
    query = _apply_period(select(func.coalesce(func.sum(Donation.amount_kopeks), 0)), start, end)
    result = await db.execute(query)
    return int(result.scalar() or 0)


async def count_donations(
    db: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Количество пожертвований (опц. за период)."""
    query = _apply_period(select(func.count(Donation.id)), start, end)
    result = await db.execute(query)
    return int(result.scalar() or 0)


async def get_donations_by_source(
    db: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, dict[str, int]]:
    """Пожертвования по источнику: сумма и количество ('menu' — кнопка, 'topup_upsell' — надбавка)."""
    query = select(
        Donation.source,
        func.coalesce(func.sum(Donation.amount_kopeks), 0).label('amount'),
        func.count(Donation.id).label('count'),
    ).group_by(Donation.source)
    result = await db.execute(_apply_period(query, start, end))
    return {
        str(source or 'menu'): {'amount_kopeks': int(amount or 0), 'count': int(count or 0)}
        for source, amount, count in result.all()
    }


async def get_donations_daily_series(
    db: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Пожертвования по дням: [{'date': 'YYYY-MM-DD', 'count': ..., 'amount_kopeks': ...}] по возрастанию даты."""
    donation_date = func.date(Donation.created_at)
    query = (
        select(
            donation_date.label('date'),
            func.count(Donation.id).label('count'),
            func.coalesce(func.sum(Donation.amount_kopeks), 0).label('amount'),
        )
        .group_by(donation_date)
        .order_by(donation_date)
    )
    result = await db.execute(_apply_period(query, start, end))
    return [
        {
            # func.date отдаёт date на PostgreSQL и str на SQLite
            'date': row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
            'count': int(row.count or 0),
            'amount_kopeks': int(row.amount or 0),
        }
        for row in result.all()
    ]


async def get_donations_daily_by_source(
    db: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Суммы пожертвований по дням и источникам: [{'date', 'source', 'amount_kopeks'}], сортировка по (дата, источник)."""
    donation_date = func.date(Donation.created_at)
    query = (
        select(
            donation_date.label('date'),
            Donation.source.label('source'),
            func.coalesce(func.sum(Donation.amount_kopeks), 0).label('amount'),
        )
        .group_by(donation_date, Donation.source)
        .order_by(donation_date, Donation.source)
    )
    result = await db.execute(_apply_period(query, start, end))
    return [
        {
            'date': row.date.isoformat() if hasattr(row.date, 'isoformat') else str(row.date),
            'source': str(row.source or 'menu'),
            'amount_kopeks': int(row.amount or 0),
        }
        for row in result.all()
    ]


async def get_user_donation_total_kopeks(db: AsyncSession, user_id: int) -> int:
    """Сумма пожертвований конкретного пользователя (по FK user_id)."""
    result = await db.execute(
        select(func.coalesce(func.sum(Donation.amount_kopeks), 0)).where(Donation.user_id == user_id)
    )
    return int(result.scalar() or 0)


async def count_distinct_donors(
    db: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Количество уникальных доноров (по coalesce(telegram_id, user_id), опц. за период)."""
    donor_key = func.coalesce(Donation.donor_telegram_id, Donation.user_id)
    query = _apply_period(select(func.count(func.distinct(donor_key))), start, end)
    result = await db.execute(query)
    return int(result.scalar() or 0)


async def get_donation_leaderboard(
    db: AsyncSession,
    *,
    limit: int = 20,
    offset: int = 0,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Топ спонсоров: группировка по донору, сортировка по сумме убыв.

    Идентичность донора — ``coalesce(donor_telegram_id, user_id)``, чтобы один и тот
    же человек не разбивался на несколько строк. Отображаемые поля (имя/username/
    user_id) берутся агрегатом ``max`` как репрезентативный снимок.
    """
    donor_key = func.coalesce(Donation.donor_telegram_id, Donation.user_id).label('donor_key')

    query = _apply_period(
        select(
            donor_key,
            func.max(Donation.user_id).label('user_id'),
            func.max(Donation.donor_telegram_id).label('donor_telegram_id'),
            func.max(Donation.donor_username).label('donor_username'),
            func.max(Donation.donor_name).label('donor_name'),
            func.count(Donation.id).label('donations_count'),
            func.sum(Donation.amount_kopeks).label('total_kopeks'),
            func.max(Donation.created_at).label('last_donation_at'),
        ),
        start,
        end,
    )
    query = query.group_by(donor_key).order_by(func.sum(Donation.amount_kopeks).desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    rows = result.all()

    leaderboard: list[dict[str, Any]] = []
    for row in rows:
        leaderboard.append(
            {
                'user_id': row.user_id,
                'donor_telegram_id': row.donor_telegram_id,
                'donor_username': row.donor_username,
                'donor_name': row.donor_name,
                'donations_count': int(row.donations_count or 0),
                'total_kopeks': int(row.total_kopeks or 0),
                'last_donation_at': row.last_donation_at,
            }
        )
    return leaderboard
