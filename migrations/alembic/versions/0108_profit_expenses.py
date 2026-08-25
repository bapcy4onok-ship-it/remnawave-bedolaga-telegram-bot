"""profit expenses: таблица расходов для расчёта чистой прибыли

Revision ID: 0108
Revises: 0107
Create Date: 2026-08-24

Добавляет ``profit_expenses`` — строки расходов (сервера, реклама, CDN,
карманные выводы, выплаты партнёрам/поддержке, ИИ-подписки), из которых
страница «Чистая прибыль» вычитает расходы из реального дохода.
Только добавление таблицы, существующие данные не трогаются.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0108'
down_revision: Union[str, None] = '0107'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'profit_expenses' in inspector.get_table_names():
        return

    op.create_table(
        'profit_expenses',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('category', sa.String(length=16), nullable=False, server_default='other'),
        sa.Column('kind', sa.String(length=16), nullable=False, server_default='once'),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('group_name', sa.String(length=128), nullable=True),
        sa.Column('link', sa.String(length=500), nullable=True),
        sa.Column('amount_minor', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='RUB'),
        sa.Column('fx_rate_snapshot', sa.Float(), nullable=True),
        sa.Column('spent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('period_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('node_uuid', sa.String(length=255), nullable=True),
        sa.Column('color', sa.String(length=16), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index('ix_profit_expenses_category_active', 'profit_expenses', ['category', 'is_active'])
    op.create_index('ix_profit_expenses_category_spent', 'profit_expenses', ['category', 'spent_at'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'profit_expenses' not in inspector.get_table_names():
        return

    op.drop_index('ix_profit_expenses_category_spent', table_name='profit_expenses')
    op.drop_index('ix_profit_expenses_category_active', table_name='profit_expenses')
    op.drop_table('profit_expenses')
