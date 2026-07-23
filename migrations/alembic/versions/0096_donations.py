"""donations: durable registry for «Support the project» donations

Revision ID: 0096
Revises: 0095
Create Date: 2026-07-23

Adds the ``donations`` table. Donations are payments made through the regular
gateways but **not** credited to the user's balance — they are recorded here (and
as a ``transactions`` row of type ``donation``). The table denormalises the donor
identity and uses ``ON DELETE SET NULL`` so the «top sponsors» statistics survive
account deletion. Additive only — nothing existing is touched.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0096'
down_revision: Union[str, None] = '0095'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'donations' in inspector.get_table_names():
        return

    op.create_table(
        'donations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column(
            'user_id',
            sa.Integer(),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('donor_telegram_id', sa.BigInteger(), nullable=True),
        sa.Column('donor_username', sa.String(length=255), nullable=True),
        sa.Column('donor_name', sa.String(length=255), nullable=True),
        sa.Column('amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('payment_method', sa.String(length=50), nullable=True),
        sa.Column('external_id', sa.String(length=255), nullable=True),
        sa.Column(
            'transaction_id',
            sa.Integer(),
            sa.ForeignKey('transactions.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('external_id', 'payment_method', name='uq_donation_external_id_method'),
    )

    op.create_index('ix_donations_donor_telegram_id', 'donations', ['donor_telegram_id'])
    op.create_index('ix_donations_user_created', 'donations', ['user_id', 'created_at'])
    op.create_index('ix_donations_created_at', 'donations', ['created_at'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'donations' not in inspector.get_table_names():
        return

    for index_name in ('ix_donations_created_at', 'ix_donations_user_created', 'ix_donations_donor_telegram_id'):
        try:
            op.drop_index(index_name, table_name='donations')
        except Exception:
            pass

    op.drop_table('donations')
