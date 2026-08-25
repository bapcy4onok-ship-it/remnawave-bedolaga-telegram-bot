"""profit expenses: доля от прибыли в процентах

Revision ID: 0109
Revises: 0108
Create Date: 2026-08-25

Добавляет ``profit_expenses.percent`` — процент от прибыли для строк вида
``kind='share'`` («мне 20 % от того, что осталось после всех расходов»).
Только добавление nullable-колонки.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0109'
down_revision: Union[str, None] = '0108'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'profit_expenses' not in inspector.get_table_names():
        return
    columns = {column['name'] for column in inspector.get_columns('profit_expenses')}
    if 'percent' not in columns:
        op.add_column('profit_expenses', sa.Column('percent', sa.Float(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'profit_expenses' not in inspector.get_table_names():
        return
    columns = {column['name'] for column in inspector.get_columns('profit_expenses')}
    if 'percent' in columns:
        op.drop_column('profit_expenses', 'percent')
