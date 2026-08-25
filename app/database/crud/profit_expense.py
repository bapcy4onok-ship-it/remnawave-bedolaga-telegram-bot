"""CRUD для строк расходов страницы «Чистая прибыль»."""

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ProfitExpense, ProfitExpenseKind


logger = structlog.get_logger(__name__)

# Виды, у которых нет даты траты: в период они попадают окном действия.
WINDOWED_KINDS = (ProfitExpenseKind.MONTHLY.value, ProfitExpenseKind.SHARE.value)


async def list_expenses(
    db: AsyncSession,
    *,
    categories: list[str] | None = None,
    include_inactive: bool = True,
) -> list[ProfitExpense]:
    """Все строки расходов (по умолчанию — вместе с выключенными, они видны в редакторе)."""
    query = select(ProfitExpense)
    if categories:
        query = query.where(ProfitExpense.category.in_(categories))
    if not include_inactive:
        query = query.where(ProfitExpense.is_active.is_(True))
    result = await db.execute(
        query.order_by(
            ProfitExpense.category,
            ProfitExpense.sort_order,
            ProfitExpense.spent_at.desc().nullslast(),
            ProfitExpense.id.desc(),
        )
    )
    return list(result.scalars().all())


async def list_expenses_for_period(
    db: AsyncSession,
    period_start: datetime,
    period_end: datetime,
    *,
    categories: list[str] | None = None,
) -> list[ProfitExpense]:
    """Строки, способные дать вклад в период.

    Регулярные отбираются по окну действия, разовые — по дате траты или по
    интервалу счёта. Точное вычисление доли — в ``profit_service``: здесь
    только грубый отсев, чтобы не тащить из БД заведомо чужие строки.
    """
    # Регулярные расходы и доли от прибыли живут не датой траты, а окном
    # действия: «этот сервер арендован с марта», «эта доля — с 1 августа».
    windowed = ProfitExpense.kind.in_(WINDOWED_KINDS)
    windowed_active = (
        windowed
        & (or_(ProfitExpense.started_at.is_(None), ProfitExpense.started_at <= period_end))
        & (or_(ProfitExpense.ended_at.is_(None), ProfitExpense.ended_at >= period_start))
    )

    once_by_date = ~windowed & ProfitExpense.spent_at.between(period_start, period_end)
    # Незакрытый интервал (есть начало, нет конца) API больше не принимает, но
    # строки, попавшие в БД раньше или мимо него, не должны становиться
    # невидимками: расход остаётся в редакторе, а в прибыль не входит —
    # ошибка, которую не на чем заметить.
    once_by_period = ~windowed & (
        ProfitExpense.period_start.isnot(None)
        & (ProfitExpense.period_start <= period_end)
        & or_(ProfitExpense.period_end.is_(None), ProfitExpense.period_end >= period_start)
    )

    query = select(ProfitExpense).where(
        ProfitExpense.is_active.is_(True),
        or_(windowed_active, once_by_date, once_by_period),
    )
    if categories:
        query = query.where(ProfitExpense.category.in_(categories))

    result = await db.execute(query.order_by(ProfitExpense.sort_order, ProfitExpense.id))
    return list(result.scalars().all())


async def get_expense(db: AsyncSession, expense_id: int) -> ProfitExpense | None:
    result = await db.execute(select(ProfitExpense).where(ProfitExpense.id == expense_id))
    return result.scalars().first()


async def get_expense_by_node(db: AsyncSession, node_uuid: str) -> ProfitExpense | None:
    """Строка расхода, привязанная к ноде панели (одна нода — одна строка)."""
    result = await db.execute(
        select(ProfitExpense).where(ProfitExpense.node_uuid == node_uuid).order_by(ProfitExpense.id)
    )
    return result.scalars().first()


async def create_expense(db: AsyncSession, **fields: Any) -> ProfitExpense:
    expense = ProfitExpense(**fields)
    db.add(expense)
    await db.commit()
    await db.refresh(expense)
    logger.info('Создана строка расхода', expense_id=expense.id, category=expense.category)
    return expense


async def update_expense(db: AsyncSession, expense: ProfitExpense, **fields: Any) -> ProfitExpense:
    for key, value in fields.items():
        setattr(expense, key, value)
    await db.commit()
    await db.refresh(expense)
    return expense


async def delete_expense(db: AsyncSession, expense_id: int) -> bool:
    result = await db.execute(delete(ProfitExpense).where(ProfitExpense.id == expense_id))
    await db.commit()
    return bool(result.rowcount)
