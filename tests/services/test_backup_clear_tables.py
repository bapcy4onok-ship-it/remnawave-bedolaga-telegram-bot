"""Восстановление бэкапа с очисткой базы: таблицы «Заданий» тоже очищаются.

Регрессия: reward_tasks и reward_task_submissions были в списке моделей бэкапа,
но не в списке очистки. Отчёты исчезали только каскадом от users, а задания,
созданные после бэкапа, переживали restore(clear_existing=True) и оставались
включёнными.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import backup_service as bs


@pytest.mark.asyncio
async def test_restore_truncates_reward_task_tables_children_first(monkeypatch):
    statements: list[str] = []

    class _Conn:
        async def execute(self, statement):
            statements.append(str(statement))
            return MagicMock()

    class _Engine:
        @contextlib.asynccontextmanager
        async def begin(self):
            yield _Conn()

        async def dispose(self):
            return None

    monkeypatch.setattr(bs, 'create_async_engine', lambda *args, **kwargs: _Engine())
    monkeypatch.setattr(bs, '_terminate_competing_backends', AsyncMock(return_value=0))

    await bs.backup_service._clear_database_tables(AsyncMock(), backup_data=None)

    [truncate] = [sql for sql in statements if sql.startswith('TRUNCATE')]
    tables = truncate.removeprefix('TRUNCATE ').split(' RESTART IDENTITY')[0].split(', ')

    reward_tables = {
        model.__tablename__
        for model in bs.backup_service.backup_models_ordered
        if model.__tablename__.startswith('reward_task')
    }
    assert reward_tables == {'reward_tasks', 'reward_task_submissions'}
    assert reward_tables <= set(tables)
    # Отчёты ссылаются на задания, пользователей и подписки — в списке они раньше них.
    assert tables.index('reward_task_submissions') < tables.index('reward_tasks') < tables.index('users')
    assert tables.index('reward_task_submissions') < tables.index('subscriptions')
