"""CRUD обещаний надбавки («добавить 10 ₽ к платежу»).

Здесь только запросы. Решение «показывать ли апселл» и подбор обещания под
зачисленный платёж — в ``app/services/donation_upsell_service.py``.
"""

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DonationPledge, DonationPledgeStatus


logger = structlog.get_logger(__name__)


async def supersede_pending_pledges(db: AsyncSession, *, user_id: int) -> int:
    """Гасит незавершённые обещания пользователя: новое решение отменяет прежние.

    Без этого брошенный платёж мог бы «прилипнуть» к следующему пополнению.
    Коммит делает вызывающая сторона (обычно сразу после — ``create_pledge``).
    """
    result = await db.execute(
        update(DonationPledge)
        .where(
            DonationPledge.user_id == user_id,
            DonationPledge.status == DonationPledgeStatus.PENDING,
        )
        .values(status=DonationPledgeStatus.SUPERSEDED)
    )
    return int(result.rowcount or 0)


async def create_pledge(
    db: AsyncSession,
    *,
    user_id: int,
    tip_kopeks: int,
    topup_amount_kopeks: int,
    payment_method: str | None,
    source: str,
    ttl_hours: int,
    status: str = DonationPledgeStatus.PENDING,
) -> DonationPledge:
    """Записывает решение пользователя по апселлу (согласие или отказ)."""
    tip_kopeks = max(0, int(tip_kopeks))
    is_pending = status == DonationPledgeStatus.PENDING

    pledge = DonationPledge(
        user_id=user_id,
        tip_kopeks=tip_kopeks,
        topup_amount_kopeks=topup_amount_kopeks,
        expected_total_kopeks=topup_amount_kopeks + tip_kopeks,
        payment_method=payment_method,
        source=source,
        status=status,
        # Ждём оплату только по согласию; отказ — просто отметка в истории решений.
        expires_at=datetime.now(UTC) + timedelta(hours=max(1, int(ttl_hours))) if is_pending else None,
    )

    db.add(pledge)
    await db.commit()
    await db.refresh(pledge)

    logger.info(
        '❤️ Записано решение по донат-апселлу',
        pledge_id=pledge.id,
        user_id=user_id,
        status=status,
        tip_kopeks=tip_kopeks,
        expected_total_kopeks=pledge.expected_total_kopeks,
        source=source,
    )
    return pledge


async def list_pending_pledges(db: AsyncSession, *, user_id: int) -> list[DonationPledge]:
    """Неистёкшие согласия пользователя, свежие первыми."""
    result = await db.execute(
        select(DonationPledge)
        .where(
            DonationPledge.user_id == user_id,
            DonationPledge.status == DonationPledgeStatus.PENDING,
            DonationPledge.expires_at > datetime.now(UTC),
        )
        .order_by(DonationPledge.created_at.desc())
    )
    return list(result.scalars().all())


async def list_applied_pledges(db: AsyncSession, *, user_id: int, limit: int = 20) -> list[DonationPledge]:
    """Исполненные надбавки пользователя, свежие первыми — для разбора прошлых платежей."""
    result = await db.execute(
        select(DonationPledge)
        .where(
            DonationPledge.user_id == user_id,
            DonationPledge.status == DonationPledgeStatus.APPLIED,
        )
        .order_by(DonationPledge.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def mark_pledge_applied(db: AsyncSession, *, pledge_id: int, transaction_id: int | None) -> bool:
    """Переводит обещание в ``applied``. ``False`` — уже применено кем-то другим.

    Условие ``status == PENDING`` внутри UPDATE — и есть защита от двойного
    списания при повторном вебхуке. Коммитит вызывающая сторона: списание баланса
    и эта отметка должны попасть в одну транзакцию.
    """
    result = await db.execute(
        update(DonationPledge)
        .where(
            DonationPledge.id == pledge_id,
            DonationPledge.status == DonationPledgeStatus.PENDING,
        )
        .values(
            status=DonationPledgeStatus.APPLIED,
            applied_at=datetime.now(UTC),
            transaction_id=transaction_id,
        )
    )
    return bool(result.rowcount)


async def mark_pledge_status(db: AsyncSession, *, pledge_id: int, status: str) -> None:
    """Ставит произвольный терминальный статус (``skipped``, ``superseded``). Без коммита.

    ``expired`` в модели зарезервирован, но никем не выставляется: срок обещания
    проверяется на лету в ``list_pending_pledges``, поэтому протухшее обещание
    неотличимо по последствиям от несуществующего, а лишний фоновый джоб не нужен.
    """
    await db.execute(update(DonationPledge).where(DonationPledge.id == pledge_id).values(status=status))


async def has_recent_decision(db: AsyncSession, *, user_id: int, days: int) -> bool:
    """Было ли решение по апселлу за последние ``days`` дней (cooldown). 0 → всегда False."""
    if days <= 0:
        return False
    since = datetime.now(UTC) - timedelta(days=days)
    result = await db.execute(
        select(func.count(DonationPledge.id)).where(
            DonationPledge.user_id == user_id,
            DonationPledge.created_at >= since,
        )
    )
    return bool(result.scalar() or 0)


async def get_upsell_funnel(
    db: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, int]:
    """Воронка апселла: показов, согласий, исполненных согласий, отказов.

    «Согласий» — все статусы, начинавшиеся с согласия (``pending`` — ждёт оплаты,
    ``applied`` — исполнено, ``superseded``/``skipped`` — согласие было, но платёж не
    дошёл). Так видно и намерение, и реальный сбор. Протухшие обещания остаются
    ``pending`` (статуса ``expired`` никто не выставляет) и считаются согласиями —
    ими они и были; на «исполнено» это не влияет.
    """
    query = select(DonationPledge.status, func.count(DonationPledge.id)).group_by(DonationPledge.status)
    if start is not None:
        query = query.where(DonationPledge.created_at >= start)
    if end is not None:
        query = query.where(DonationPledge.created_at <= end)

    result = await db.execute(query)
    counts = {str(status): int(count or 0) for status, count in result.all()}

    declined = counts.get(DonationPledgeStatus.DECLINED, 0)
    shown = sum(counts.values())
    return {
        'shown': shown,
        'accepted': shown - declined,
        'applied': counts.get(DonationPledgeStatus.APPLIED, 0),
        'declined': declined,
    }
