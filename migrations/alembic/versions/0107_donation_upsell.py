"""donation upsell: pledges table + donations.source

Revision ID: 0107
Revises: 0106
Create Date: 2026-08-19

Добавляет таблицу ``donation_pledges`` (обещание надбавки к платежу при пополнении)
и колонку ``donations.source`` (откуда пришло пожертвование: меню или апселл).
Только добавление — существующие данные не меняются, старые донаты получают
``source='menu'`` через server_default.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0107'
down_revision: Union[str, None] = '0106'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'donations' in inspector.get_table_names():
        donation_columns = {column['name'] for column in inspector.get_columns('donations')}
        if 'source' not in donation_columns:
            op.add_column(
                'donations',
                sa.Column('source', sa.String(length=32), nullable=False, server_default='menu'),
            )
            op.create_index('ix_donations_source', 'donations', ['source'])

    if 'donation_pledges' in inspector.get_table_names():
        return

    op.create_table(
        'donation_pledges',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tip_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('topup_amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('expected_total_kopeks', sa.Integer(), nullable=False),
        sa.Column('payment_method', sa.String(length=50), nullable=True),
        sa.Column('source', sa.String(length=16), nullable=False, server_default='bot'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'transaction_id',
            sa.Integer(),
            sa.ForeignKey('transactions.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )

    op.create_index('ix_donation_pledges_user_status', 'donation_pledges', ['user_id', 'status', 'created_at'])
    op.create_index('ix_donation_pledges_status_expires', 'donation_pledges', ['status', 'expires_at'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'donation_pledges' in inspector.get_table_names():
        op.drop_index('ix_donation_pledges_status_expires', table_name='donation_pledges')
        op.drop_index('ix_donation_pledges_user_status', table_name='donation_pledges')
        op.drop_table('donation_pledges')

    if 'donations' in inspector.get_table_names():
        donation_columns = {column['name'] for column in inspector.get_columns('donations')}
        if 'source' in donation_columns:
            op.drop_index('ix_donations_source', table_name='donations')
            op.drop_column('donations', 'source')
