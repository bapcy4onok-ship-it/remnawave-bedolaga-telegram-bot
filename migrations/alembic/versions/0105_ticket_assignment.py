"""tickets: assignment ("take ticket") + closed_by

Revision ID: 0105
Revises: 0104
Create Date: 2026-07-08

Adds moderator assignment columns to ``tickets``: who took the ticket,
when, and who closed it (for moderator rank counters). Additive only —
the existing ticket workflow is untouched.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0105'
down_revision: Union[str, None] = '0104'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'tickets' in inspector.get_table_names():
        existing = {col['name'] for col in inspector.get_columns('tickets')}
        if 'assigned_to_user_id' not in existing:
            op.add_column(
                'tickets',
                sa.Column(
                    'assigned_to_user_id',
                    sa.Integer(),
                    sa.ForeignKey('users.id', ondelete='SET NULL'),
                    nullable=True,
                ),
            )
        if 'assigned_at' not in existing:
            op.add_column('tickets', sa.Column('assigned_at', sa.DateTime(timezone=True), nullable=True))
        if 'closed_by_user_id' not in existing:
            op.add_column(
                'tickets',
                sa.Column(
                    'closed_by_user_id',
                    sa.Integer(),
                    sa.ForeignKey('users.id', ondelete='SET NULL'),
                    nullable=True,
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'tickets' in inspector.get_table_names():
        existing = {col['name'] for col in inspector.get_columns('tickets')}
        for column in ('closed_by_user_id', 'assigned_at', 'assigned_to_user_id'):
            if column in existing:
                op.drop_column('tickets', column)
