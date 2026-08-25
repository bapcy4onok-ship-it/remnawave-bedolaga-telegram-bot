"""profit expenses: одна строка стоимости на ноду панели

Revision ID: 0110
Revises: 0109
Create Date: 2026-08-25

Быстрый ввод цен в диалоге серверов мог создать две строки на одну ноду:
POST уходил повторно, пока не пришёл рефетч, а дубль не был виден — диалог
схлопывает строки по ``node_uuid``. Аренда при этом считалась дважды.

Перед созданием индекса уже существующие дубли обезвреживаются: у всех строк,
кроме самой ранней по каждой ноде, ``node_uuid`` обнуляется. Строки не удаляются —
это чужие деньги, пусть админ сам решит, что с ними делать; они останутся видны
в разделе «вне панели».
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0110'
down_revision: Union[str, None] = '0109'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = 'uq_profit_expenses_node_uuid'


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'profit_expenses' not in inspector.get_table_names():
        return
    if _INDEX in {index['name'] for index in inspector.get_indexes('profit_expenses')}:
        return

    op.execute(
        sa.text(
            """
            UPDATE profit_expenses
               SET node_uuid = NULL
             WHERE node_uuid IS NOT NULL
               AND id NOT IN (
                   SELECT MIN(id) FROM profit_expenses
                    WHERE node_uuid IS NOT NULL
                    GROUP BY node_uuid
               )
            """
        )
    )

    op.create_index(
        _INDEX,
        'profit_expenses',
        ['node_uuid'],
        unique=True,
        postgresql_where=sa.text('node_uuid IS NOT NULL'),
        sqlite_where=sa.text('node_uuid IS NOT NULL'),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'profit_expenses' not in inspector.get_table_names():
        return
    if _INDEX in {index['name'] for index in inspector.get_indexes('profit_expenses')}:
        op.drop_index(_INDEX, table_name='profit_expenses')
