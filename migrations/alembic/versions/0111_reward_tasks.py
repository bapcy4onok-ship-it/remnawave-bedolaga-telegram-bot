"""reward tasks: задания за бесплатные дни подписки

Revision ID: 0111
Revises: 0110
Create Date: 2026-10-08

Добавляет ``reward_tasks`` (задания: фотоотчёт или буст канала) и
``reward_task_submissions`` (попытки их выполнить). Частичные уникальные индексы
по (task_id, user_id) для статусов ``approved`` и ``pending`` — это и есть защита
от двойной награды и двух отчётов на проверке одновременно: сервис
(``app/services/reward_task_service.py``) на них опирается. Только добавление
таблиц, существующие данные не трогаются.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '0111'
down_revision: Union[str, None] = '0110'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TASKS = 'reward_tasks'
_SUBMISSIONS = 'reward_task_submissions'

# (имя, колонки, unique, условие частичного индекса)
_SUBMISSION_INDEXES = (
    ('ix_reward_task_submissions_status_created', ['status', 'created_at'], False, None),
    ('ix_reward_task_submissions_user_id', ['user_id'], False, None),
    ('ix_reward_task_submissions_task_id', ['task_id'], False, None),
    ('uq_reward_task_submissions_approved', ['task_id', 'user_id'], True, "status = 'approved'"),
    ('uq_reward_task_submissions_pending', ['task_id', 'user_id'], True, "status = 'pending'"),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if _TASKS not in tables:
        op.create_table(
            _TASKS,
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('kind', sa.String(length=32), nullable=False),
            sa.Column('title', sa.String(length=200), nullable=False),
            sa.Column('description', sa.Text(), nullable=False, server_default=''),
            sa.Column('reward_days', sa.Integer(), nullable=False),
            sa.Column('action_url', sa.String(length=500), nullable=True),
            sa.Column('channel_id', sa.String(length=100), nullable=True),
            sa.Column('min_boosts', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('max_photos', sa.Integer(), nullable=False, server_default='3'),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint('reward_days BETWEEN 1 AND 365', name='ck_reward_tasks_reward_days'),
            sa.CheckConstraint('max_photos BETWEEN 1 AND 10', name='ck_reward_tasks_max_photos'),
            sa.CheckConstraint('min_boosts BETWEEN 1 AND 100', name='ck_reward_tasks_min_boosts'),
        )

    if _SUBMISSIONS not in tables:
        op.create_table(
            _SUBMISSIONS,
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'task_id',
                sa.Integer(),
                sa.ForeignKey('reward_tasks.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False),
            sa.Column('source', sa.String(length=20), nullable=False),
            sa.Column('media_items', JSONB, nullable=False, server_default='[]'),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('boost_count', sa.Integer(), nullable=True),
            sa.Column('reward_days', sa.Integer(), nullable=True),
            sa.Column(
                'subscription_id',
                sa.Integer(),
                sa.ForeignKey('subscriptions.id', ondelete='SET NULL'),
                nullable=True,
            ),
            sa.Column('reject_reason', sa.Text(), nullable=True),
            sa.Column('reviewed_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
            sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    existing_indexes = {index['name'] for index in sa.inspect(bind).get_indexes(_SUBMISSIONS)}
    for name, columns, unique, where in _SUBMISSION_INDEXES:
        if name in existing_indexes:
            continue
        kwargs = {}
        if where is not None:
            kwargs = {'postgresql_where': sa.text(where), 'sqlite_where': sa.text(where)}
        op.create_index(name, _SUBMISSIONS, columns, unique=unique, **kwargs)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if _SUBMISSIONS in tables:
        existing_indexes = {index['name'] for index in inspector.get_indexes(_SUBMISSIONS)}
        for name, _columns, _unique, _where in reversed(_SUBMISSION_INDEXES):
            if name in existing_indexes:
                op.drop_index(name, table_name=_SUBMISSIONS)
        op.drop_table(_SUBMISSIONS)

    if _TASKS in tables:
        op.drop_table(_TASKS)
