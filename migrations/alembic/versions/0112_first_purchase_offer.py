"""Guarantee one first-purchase offer per user, even across workers/restarts."""

from alembic import op
import sqlalchemy as sa


revision = '0112'
down_revision = '0111'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        'uq_first_purchase_offer_user',
        'discount_offers',
        ['user_id'],
        unique=True,
        postgresql_where=sa.text("notification_type = 'first_purchase_48h'"),
        sqlite_where=sa.text("notification_type = 'first_purchase_48h'"),
    )


def downgrade():
    op.drop_index('uq_first_purchase_offer_user', table_name='discount_offers')
