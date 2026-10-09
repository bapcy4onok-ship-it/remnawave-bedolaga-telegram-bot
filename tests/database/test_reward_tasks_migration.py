"""Миграция 0111 (reward tasks): цепочка ревизий, симметричность upgrade/downgrade
и совпадение схемы с models.py.

Свежие БД создаются через ``create_all`` + stamp head, обновляемые — миграцией,
поэтому индексы и ограничения обязаны совпадать в обоих путях. Гоняем на
SQLite: частичные индексы там поддерживаются (``sqlite_where``), JSONB
рендерится как JSON (``tests/fixtures/sqlite_memory``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

import tests.fixtures.sqlite_memory  # noqa: F401  (регистрирует JSONB → JSON для SQLite)
from app.database.models import Base, RewardTask, RewardTaskSubmission, Subscription, Tariff, User


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / 'migrations' / 'alembic' / 'versions' / '0111_reward_tasks.py'
TABLES = ('reward_tasks', 'reward_task_submissions')


def _load_migration():
    spec = importlib.util.spec_from_file_location('migration_0111_reward_tasks', MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine_with_base_tables() -> sa.Engine:
    engine = sa.create_engine('sqlite://')
    Base.metadata.create_all(engine, tables=[User.__table__, Tariff.__table__, Subscription.__table__])
    return engine


def _run(engine: sa.Engine, step) -> None:
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            step()


def _schema(engine: sa.Engine) -> dict:
    inspector = sa.inspect(engine)
    schema = {}
    for table in TABLES:
        schema[table] = {
            'columns': {
                column['name']: (column['nullable'], column['default'] is not None)
                for column in inspector.get_columns(table)
            },
            'indexes': {
                index['name']: (
                    tuple(index['column_names']),
                    bool(index['unique']),
                    str((index.get('dialect_options') or {}).get('sqlite_where', '')),
                )
                for index in inspector.get_indexes(table)
            },
            'checks': {check['name']: check['sqltext'] for check in inspector.get_check_constraints(table)},
            'foreign_keys': {
                (tuple(fk['constrained_columns']), fk['referred_table'], (fk.get('options') or {}).get('ondelete'))
                for fk in inspector.get_foreign_keys(table)
            },
        }
    return schema


def test_revision_chain_has_no_branch():
    migration = _load_migration()
    assert migration.revision == '0111'
    assert migration.down_revision == '0110'

    config = Config(str(ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(ROOT / 'migrations' / 'alembic'))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert [rev.revision for rev in script.walk_revisions() if rev.down_revision == '0110'] == ['0111']


def test_upgrade_matches_models_and_downgrade_is_symmetric():
    migration = _load_migration()

    migrated = _engine_with_base_tables()
    _run(migrated, migration.upgrade)
    _run(migrated, migration.upgrade)  # повторный прогон — no-op благодаря проверкам инспектора

    from_models = _engine_with_base_tables()
    Base.metadata.create_all(from_models, tables=[RewardTask.__table__, RewardTaskSubmission.__table__])

    migrated_schema = _schema(migrated)
    assert migrated_schema == _schema(from_models)

    submissions = migrated_schema['reward_task_submissions']
    assert submissions['indexes']['uq_reward_task_submissions_approved'][:2] == (('task_id', 'user_id'), True)
    assert submissions['indexes']['uq_reward_task_submissions_pending'][:2] == (('task_id', 'user_id'), True)
    assert (('task_id',), 'reward_tasks', 'CASCADE') in submissions['foreign_keys']
    assert (('subscription_id',), 'subscriptions', 'SET NULL') in submissions['foreign_keys']
    assert set(migrated_schema['reward_tasks']['checks']) == {
        'ck_reward_tasks_reward_days',
        'ck_reward_tasks_max_photos',
        'ck_reward_tasks_min_boosts',
    }

    _run(migrated, migration.downgrade)
    remaining = set(sa.inspect(migrated).get_table_names())
    assert not remaining & set(TABLES)
    assert {'users', 'subscriptions'} <= remaining
    _run(migrated, migration.downgrade)  # повторный откат — no-op

    # После отката миграция снова применяется начисто.
    _run(migrated, migration.upgrade)
    assert _schema(migrated) == migrated_schema


def test_partial_indexes_enforce_one_approved_and_one_pending():
    migration = _load_migration()
    engine = _engine_with_base_tables()
    _run(engine, migration.upgrade)

    def insert(connection, status: str) -> None:
        connection.execute(
            sa.text(
                'INSERT INTO reward_task_submissions (task_id, user_id, status, source) VALUES (1, 1, :status, :src)'
            ),
            {'status': status, 'src': 'bot'},
        )

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users (id, auth_type, has_had_paid_subscription, email_verified, has_made_first_topup, auto_promo_group_assigned, auto_promo_group_threshold_kopeks, promo_offer_discount_percent, restriction_topup, restriction_subscription, partner_status) VALUES (1, 'telegram', 0, 0, 0, 0, 0, 0, 0, 0, 'none')"
            )
        )
        connection.execute(
            sa.text("INSERT INTO reward_tasks (id, kind, title, reward_days) VALUES (1, 'photo_report', 't', 1)")
        )
        for status in ('rejected', 'rejected', 'pending', 'approved'):
            insert(connection, status)
        row = connection.execute(
            sa.text('SELECT media_items, description FROM reward_task_submissions, reward_tasks LIMIT 1')
        ).one()
        assert row == ('[]', '')

    for status in ('pending', 'approved'):
        try:
            with engine.begin() as connection:
                insert(connection, status)
        except sa.exc.IntegrityError:
            continue
        raise AssertionError(f'second {status} row must violate the partial unique index')

    for values in ("('channel_boost', 't', 0)", "('channel_boost', 't', 366)"):
        try:
            with engine.begin() as connection:
                connection.execute(sa.text(f'INSERT INTO reward_tasks (kind, title, reward_days) VALUES {values}'))
        except sa.exc.IntegrityError:
            continue
        raise AssertionError(f'reward_days CHECK must reject {values}')
