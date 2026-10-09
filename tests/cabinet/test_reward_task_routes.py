"""Кабинетные роуты «Заданий»: регистрация, права, формат ошибок и сквозной HTTP.

HTTP-тесты гоняют настоящий FastAPI-стек (валидация тела, response_model,
пустой путь списка при ``redirect_slashes=False``) поверх in-memory SQLite;
проверка прав и аудит заглушены, панель/Lava/Telegram — фейки.

Скрытый раздел (отключённая подписка, режим «только админы» —
REWARD_TASKS_FOR_ALL_USERS выключен): каждый пользовательский роут отвечает
404 ``tasks_unavailable``, счётчик — 0. Переключатель меняется настоящим
роутом админ-настроек кабинета и действует без перезапуска.
"""

from __future__ import annotations

import contextlib
import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.config import settings
from app.database.models import (
    AdminRole,
    RewardTask,
    RewardTaskSubmission,
    SentNotification,
    ServerSquad,
    Subscription,
    SystemSetting,
    Tariff,
    TrafficPurchase,
    User,
    UserRole,
)
from app.services import reward_task_service
from app.services.reward_task_service import RewardTaskError
from app.services.subscription_service import SubscriptionService
from app.utils.cache import RateLimitCache


USER_ID = 1
ADMIN_ID = 2
ADMIN_TELEGRAM_ID = 999
FILE_A = 'AgACAgIAAxkBAAIBQ2ZxAAAAAAAAAAAAAAAAAAAA'
FILE_B = 'AgACAgIAAxkBAAIBQ2ZxBBBBBBBBBBBBBBBBBBBB'
SWITCH = 'REWARD_TASKS_FOR_ALL_USERS'
UNAVAILABLE = {'code': 'tasks_unavailable', 'message': 'Раздел недоступен'}


@pytest.fixture(autouse=True)
def _tasks_for_all_users(monkeypatch: pytest.MonkeyPatch) -> None:
    """По умолчанию раздел открыт всем; тесты режима «только админы» выключают его сами."""
    monkeypatch.setattr(settings, SWITCH, True)


# --- Wiring ----------------------------------------------------------------


def test_reward_task_routes_registered(registered_paths) -> None:
    assert registered_paths.get('/cabinet/tasks') == {'GET'}
    assert registered_paths.get('/cabinet/tasks/count') == {'GET'}
    assert registered_paths.get('/cabinet/tasks/{task_id}/submissions') == {'POST'}
    assert registered_paths.get('/cabinet/tasks/{task_id}/check-boost') == {'POST'}
    assert registered_paths.get('/cabinet/admin/tasks') == {'GET', 'POST'}
    assert registered_paths.get('/cabinet/admin/tasks/{task_id}') == {'PATCH', 'DELETE'}
    assert registered_paths.get('/cabinet/admin/tasks/submissions') == {'GET'}
    assert registered_paths.get('/cabinet/admin/tasks/submissions/approve') == {'POST'}
    assert registered_paths.get('/cabinet/admin/tasks/submissions/reject') == {'POST'}


def test_tasks_permissions_registered_and_preset() -> None:
    from app.services.permission_service import PERMISSION_REGISTRY, get_all_permissions
    from app.services.rbac_bootstrap_service import _PRESET_ROLES

    assert PERMISSION_REGISTRY['tasks'] == ['read', 'edit', 'approve', 'reject']
    for permission in ('tasks:read', 'tasks:edit', 'tasks:approve', 'tasks:reject'):
        assert permission in get_all_permissions()
    presets = {role['name']: role['permissions'] for role in _PRESET_ROLES}
    assert 'tasks:*' in presets['Admin']
    assert 'tasks:*' in presets['Marketer']
    assert 'tasks:*' not in presets['Support']


def test_admin_routes_require_the_right_permission() -> None:
    """Каждый админ-роут закрыт своим правом (замыкание require_permission хранит список)."""
    from app.cabinet.routes.admin_reward_tasks import router

    expected = {
        ('GET', '/admin/tasks/submissions'): ('tasks:read',),
        ('POST', '/admin/tasks/submissions/approve'): ('tasks:approve',),
        ('POST', '/admin/tasks/submissions/reject'): ('tasks:reject',),
        ('GET', '/admin/tasks'): ('tasks:read',),
        ('POST', '/admin/tasks'): ('tasks:edit',),
        ('PATCH', '/admin/tasks/{task_id}'): ('tasks:edit',),
        ('DELETE', '/admin/tasks/{task_id}'): ('tasks:edit',),
    }
    seen = {}
    for route in router.routes:
        for method in route.methods:
            for dependency in route.dependant.dependencies:
                closure = getattr(dependency.call, '__closure__', None) or ()
                for cell in closure:
                    if isinstance(cell.cell_contents, tuple) and all(':' in p for p in cell.cell_contents):
                        seen[(method, route.path)] = cell.cell_contents
    assert seen == expected


# --- Error contract --------------------------------------------------------


@pytest.mark.parametrize(
    ('code', 'params', 'status_code'),
    [
        ('task_not_found', {}, 404),
        ('task_inactive', {}, 404),
        ('already_completed', {}, 409),
        ('already_pending', {}, 409),
        ('no_subscription', {}, 400),
        ('restricted', {}, 403),
        ('tasks_unavailable', {}, 404),
        ('subscription_disabled', {}, 403),
        ('user_blocked', {}, 403),
        ('invalid_media', {}, 400),
        ('too_many_media', {'max': 3}, 400),
        ('telegram_required', {}, 400),
        ('not_enough_boosts', {'found': 0, 'required': 1}, 400),
        ('rate_limited', {}, 429),
        ('boost_check_failed', {}, 502),
        ('panel_sync_failed', {}, 503),
        ('internal_error', {}, 500),
    ],
)
def test_error_detail_contract(code, params, status_code) -> None:
    from app.cabinet.routes.reward_tasks import reward_task_http_error

    exc = reward_task_http_error(RewardTaskError(code, **params), 'ru')
    assert exc.status_code == status_code
    assert exc.detail['code'] == code
    assert exc.detail['message']
    for key, value in params.items():
        assert exc.detail[key] == value


def test_params_cannot_override_message() -> None:
    from app.cabinet.routes.reward_tasks import reward_task_http_error

    exc = reward_task_http_error(RewardTaskError('invalid_media', message='x'), 'en')
    assert exc.detail['code'] == 'invalid_media'
    assert exc.detail['message'] == 'Could not accept the screenshots. Please upload them again.'


@pytest.mark.asyncio
async def test_check_boost_route_closes_its_bot_and_maps_errors(monkeypatch) -> None:
    from app.cabinet.routes import reward_tasks as routes

    bot = MagicMock()
    bot.__aenter__ = AsyncMock(return_value=bot)
    bot.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(routes, 'create_bot', lambda: bot)
    monkeypatch.setattr(
        routes.reward_task_service,
        'get_reward_tasks_access',
        AsyncMock(return_value=reward_task_service.RewardTasksAccess(visible=True)),
    )
    task = SimpleNamespace(id=5, kind='channel_boost', is_active=True)
    monkeypatch.setattr(routes.reward_task_service, 'require_active_task', AsyncMock(return_value=task))
    check = AsyncMock(side_effect=RewardTaskError('not_enough_boosts', found=0, required=1))
    monkeypatch.setattr(routes.reward_task_service, 'check_boost', check)
    user = SimpleNamespace(id=1, language='en')

    with pytest.raises(HTTPException) as error:
        await routes.check_reward_task_boost(task_id=5, user=user, db=AsyncMock())

    assert error.value.status_code == 400
    assert error.value.detail == {
        'found': 0,
        'required': 1,
        'code': 'not_enough_boosts',
        'message': 'Boosts found: 0 of 1. Boost the channel and check again.',
    }
    assert check.await_args.args[3] is bot
    bot.__aexit__.assert_awaited_once()

    check.side_effect = None
    check.return_value = SimpleNamespace(reward_days=3)
    response = await routes.check_reward_task_boost(task_id=5, user=user, db=AsyncMock())
    assert response.model_dump() == {'completed': True, 'reward_days': 3}


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['subscription_disabled', 'tasks_unavailable'])
async def test_user_routes_answer_a_race_into_the_hidden_section_with_404(monkeypatch, code) -> None:
    """Подписку отключили между проверкой видимости и начислением: перепроверка под
    блокировкой отвечает subscription_disabled. Пользователь уже скрыт — роут отдаёт
    404 tasks_unavailable, как и остальным входам скрытого раздела, а не 403."""
    from app.cabinet.routes import reward_tasks as routes
    from app.cabinet.schemas.reward_tasks import RewardTaskSubmitRequest

    bot = MagicMock()
    bot.__aenter__ = AsyncMock(return_value=bot)
    bot.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(routes, 'create_bot', lambda: bot)
    monkeypatch.setattr(
        routes.reward_task_service,
        'get_reward_tasks_access',
        AsyncMock(return_value=reward_task_service.RewardTasksAccess(visible=True)),
    )
    task = SimpleNamespace(id=5, kind='channel_boost', is_active=True)
    monkeypatch.setattr(routes.reward_task_service, 'require_active_task', AsyncMock(return_value=task))
    monkeypatch.setattr(routes.reward_task_service, 'check_boost', AsyncMock(side_effect=RewardTaskError(code)))
    monkeypatch.setattr(routes.reward_task_service, 'submit_photo_report', AsyncMock(side_effect=RewardTaskError(code)))
    user = SimpleNamespace(id=1, language='ru')
    payload = RewardTaskSubmitRequest(media=[{'file_id': FILE_A}])

    for call in (
        routes.check_reward_task_boost(task_id=5, user=user, db=AsyncMock()),
        routes.submit_reward_task_report(task_id=5, payload=payload, user=user, db=AsyncMock()),
    ):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 404
        assert error.value.detail == UNAVAILABLE


# --- Media upload returns a thumbnail --------------------------------------


@pytest.mark.asyncio
async def test_photo_upload_returns_thumbnail_file_id(monkeypatch) -> None:
    from starlette.datastructures import Headers, UploadFile

    from app.cabinet.routes import media

    sizes = [
        SimpleNamespace(file_id='size90', file_unique_id='u90', width=90),
        SimpleNamespace(file_id='size320', file_unique_id='u320', width=320),
        SimpleNamespace(file_id='size1280', file_unique_id='u1280', width=1280),
    ]
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(photo=sizes, message_id=11))
    bot.delete_message = AsyncMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(media, 'create_bot', lambda: bot)
    monkeypatch.setattr(media, '_resolve_target_chat_id', lambda: -100)
    monkeypatch.setattr(media, '_build_media_url', lambda request, file_id: f'/m/{file_id}')

    upload = UploadFile(
        file=io.BytesIO(b'\x89PNG fake'), filename='shot.png', headers=Headers({'content-type': 'image/png'})
    )
    response = await media.upload_media(
        request=MagicMock(), user=SimpleNamespace(telegram_id=1), file=upload, media_type='photo'
    )

    assert response.file_id == 'size1280'  # полный размер — как и раньше
    assert response.file_unique_id == 'u1280'
    assert response.thumbnail_file_id == 'size320'
    bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_document_upload_has_no_thumbnail(monkeypatch) -> None:
    from starlette.datastructures import Headers, UploadFile

    from app.cabinet.routes import media

    bot = MagicMock()
    bot.send_document = AsyncMock(
        return_value=SimpleNamespace(document=SimpleNamespace(file_id='doc', file_unique_id='ud'), message_id=1)
    )
    bot.delete_message = AsyncMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(media, 'create_bot', lambda: bot)
    monkeypatch.setattr(media, '_resolve_target_chat_id', lambda: -100)
    monkeypatch.setattr(media, '_build_media_url', lambda request, file_id: f'/m/{file_id}')

    upload = UploadFile(
        file=io.BytesIO(b'%PDF-1.4'), filename='a.pdf', headers=Headers({'content-type': 'application/pdf'})
    )
    response = await media.upload_media(
        request=MagicMock(), user=SimpleNamespace(telegram_id=1), file=upload, media_type='document'
    )
    assert response.file_id == 'doc'
    assert response.thumbnail_file_id is None


# --- HTTP end to end -------------------------------------------------------

TABLES = [
    User.__table__,
    Tariff.__table__,
    Subscription.__table__,
    TrafficPurchase.__table__,
    SentNotification.__table__,
    RewardTask.__table__,
    RewardTaskSubmission.__table__,
    # Подписке без сквадов награда ищет сквады заранее (на случай создания в панели).
    ServerSquad.__table__,
    # Режим «только админы»: роли кабинета и правка настроек через админку.
    AdminRole.__table__,
    UserRole.__table__,
    SystemSetting.__table__,
]


@contextlib.asynccontextmanager
async def _client(monkeypatch, *, as_admin: bool, panel_ok: bool = True):
    import httpx
    from fastapi import FastAPI

    from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
    from app.cabinet.routes import reward_tasks as user_routes
    from app.cabinet.routes.admin_reward_tasks import router as admin_router
    from app.services.payment import lava as lava_module
    from app.services.permission_service import PermissionService
    from tests.fixtures.sqlite_memory import memory_session

    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(True, None)), raising=False)
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock(return_value=None), raising=False)
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(lava_module, 'shift_lava_next_charge_after_manual_extension', AsyncMock())
    monkeypatch.setattr(RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False))

    async def fake_sync(self, db, subscription, **kwargs):
        if not panel_ok:
            await db.rollback()
            return None
        await db.commit()
        return SimpleNamespace(id=4242)

    monkeypatch.setattr(SubscriptionService, 'update_remnawave_user', fake_sync)
    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', fake_sync)

    @contextlib.asynccontextmanager
    async def _panel_client(self):
        # Перед обновлением сервис спрашивает панель, жив ли её пользователь (на 404 — создание).
        yield SimpleNamespace(get_user_by_id=AsyncMock(side_effect=lambda panel_id: SimpleNamespace(id=panel_id)))

    monkeypatch.setattr(SubscriptionService, 'get_api_client', _panel_client)

    notify_bot = MagicMock()
    notify_bot.send_message = AsyncMock()
    notify_bot.session.close = AsyncMock()
    monkeypatch.setattr(reward_task_service, 'create_bot', lambda: notify_bot)

    boost_bot = MagicMock()
    boost_bot.__aenter__ = AsyncMock(return_value=boost_bot)
    boost_bot.__aexit__ = AsyncMock(return_value=False)
    boost_bot.get_user_chat_boosts = AsyncMock(
        return_value=SimpleNamespace(boosts=[SimpleNamespace(expiration_date=datetime.now(UTC) + timedelta(days=9))])
    )
    monkeypatch.setattr(user_routes, 'create_bot', lambda: boost_bot)
    reward_task_service.invalidate_active_tasks_cache()

    async with memory_session(monkeypatch, TABLES) as session:
        now = datetime.now(UTC)
        session.add_all(
            [
                User(id=USER_ID, telegram_id=555, username='alice', first_name='Alice', language='ru', remnawave_id=1),
                User(id=ADMIN_ID, telegram_id=ADMIN_TELEGRAM_ID, first_name='Admin', language='ru'),
                Subscription(
                    id=10,
                    user_id=USER_ID,
                    status='active',
                    is_trial=False,
                    end_date=now + timedelta(days=5),
                    traffic_limit_gb=0,
                    remnawave_short_id='s10',
                ),
            ]
        )
        await session.commit()

        async def current_user():
            return await session.get(User, ADMIN_ID if as_admin else USER_ID)

        app = FastAPI()
        app.include_router(user_routes.router, prefix='/cabinet')
        app.include_router(admin_router, prefix='/cabinet')
        app.dependency_overrides[get_cabinet_db] = lambda: session
        app.dependency_overrides[get_current_cabinet_user] = current_user

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            yield SimpleNamespace(client=client, session=session, notify_bot=notify_bot, boost_bot=boost_bot)


async def _create_tasks(client) -> tuple[int, int]:
    photo = await client.post(
        '/cabinet/admin/tasks',
        json={
            'kind': 'photo_report',
            'title': 'Лайки',
            'description': 'Поставьте лайки',
            'reward_days': 1,
            'action_url': 'https://t.me/ledokol',
            'channel_id': None,
            'min_boosts': 1,
            'max_photos': 2,
            'is_active': True,
            'sort_order': 2,
        },
    )
    assert photo.status_code == 201, photo.text
    boost = await client.post(
        '/cabinet/admin/tasks',
        json={'kind': 'channel_boost', 'title': 'Буст', 'reward_days': 3, 'channel_id': '3822976183', 'sort_order': 1},
    )
    assert boost.status_code == 201, boost.text
    assert boost.json()['channel_id'] == '-1003822976183'
    return photo.json()['id'], boost.json()['id']


@pytest.mark.asyncio
async def test_http_admin_task_crud(monkeypatch) -> None:
    async with _client(monkeypatch, as_admin=True) as env:
        photo_id, boost_id = await _create_tasks(env.client)

        bad = await env.client.post(
            '/cabinet/admin/tasks', json={'kind': 'channel_boost', 'title': 'x', 'reward_days': 3}
        )
        assert bad.status_code == 422
        assert 'channel_id' in bad.text

        listed = await env.client.get('/cabinet/admin/tasks')
        assert listed.status_code == 200
        items = listed.json()['items']
        assert [item['id'] for item in items] == [boost_id, photo_id]  # sort_order, id
        assert items[1]['pending_count'] == 0

        patched = await env.client.patch(
            f'/cabinet/admin/tasks/{photo_id}', json={'title': 'Новое', 'pending_count': 9}
        )
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body['title'] == 'Новое'
        assert body['description'] == 'Поставьте лайки'  # непереданное — как было
        assert body['updated_at']

        invalid_patch = await env.client.patch(f'/cabinet/admin/tasks/{photo_id}', json={'reward_days': 0})
        assert invalid_patch.status_code == 422
        null_patch = await env.client.patch(f'/cabinet/admin/tasks/{photo_id}', json={'title': None})
        assert null_patch.status_code == 422

        missing = await env.client.patch('/cabinet/admin/tasks/9999', json={'title': 'x'})
        assert missing.status_code == 404

        deleted = await env.client.delete(f'/cabinet/admin/tasks/{boost_id}')
        assert deleted.status_code == 204
        assert (await env.client.get('/cabinet/admin/tasks')).json()['items'][0]['id'] == photo_id


@pytest.mark.asyncio
async def test_http_user_flow_and_admin_review(monkeypatch) -> None:
    async with _client(monkeypatch, as_admin=True) as admin_env:
        photo_id, boost_id = await _create_tasks(admin_env.client)
        session = admin_env.session

        # Пользовательская часть — тот же сеанс БД, другой текущий пользователь.
        import httpx
        from fastapi import FastAPI

        from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
        from app.cabinet.routes import reward_tasks as user_routes

        user_app = FastAPI()
        user_app.include_router(user_routes.router, prefix='/cabinet')
        user_app.dependency_overrides[get_cabinet_db] = lambda: session

        async def current_user():
            return await session.get(User, USER_ID)

        user_app.dependency_overrides[get_current_cabinet_user] = current_user
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=user_app), base_url='http://test') as user:
            count = await user.get('/cabinet/tasks/count')
            assert count.json() == {'count': 2}

            tasks = await user.get('/cabinet/tasks')
            assert tasks.status_code == 200
            body = tasks.json()
            assert body['eligible'] is True
            assert body['ineligible_reason'] is None
            assert [(t['id'], t['status']) for t in body['tasks']] == [(boost_id, 'available'), (photo_id, 'available')]
            assert 'channel_id' not in body['tasks'][0]

            wrong_kind = await user.post(
                f'/cabinet/tasks/{boost_id}/submissions', json={'media': [{'file_id': FILE_A}]}
            )
            assert wrong_kind.status_code == 400
            assert wrong_kind.json()['detail']['code'] == 'wrong_task_kind'

            unknown = await user.post('/cabinet/tasks/9999/submissions', json={'media': [{'file_id': FILE_A}]})
            assert unknown.status_code == 404
            assert unknown.json()['detail']['code'] == 'task_not_found'

            too_many = await user.post(
                f'/cabinet/tasks/{photo_id}/submissions',
                json={'media': [{'file_id': FILE_A}, {'file_id': FILE_B}, {'file_id': FILE_A[:-1] + 'C'}]},
            )
            assert too_many.status_code == 400
            assert too_many.json()['detail'] == {
                'max': 2,
                'code': 'too_many_media',
                'message': 'Слишком много скриншотов — можно не больше 2.',
            }

            created = await user.post(
                f'/cabinet/tasks/{photo_id}/submissions',
                json={'media': [{'file_id': FILE_A, 'thumb_file_id': FILE_B}], 'comment': 'вот'},
            )
            assert created.status_code == 201, created.text
            submission = created.json()
            assert submission['status'] == 'pending'
            assert submission['created_at']

            again = await user.post(f'/cabinet/tasks/{photo_id}/submissions', json={'media': [{'file_id': FILE_B}]})
            assert again.status_code == 409
            assert again.json()['detail']['code'] == 'already_pending'
            assert again.json()['detail']['message'] == 'Ваш отчёт по этому заданию уже на проверке.'

            boost = await user.post(f'/cabinet/tasks/{boost_id}/check-boost')
            assert boost.status_code == 200, boost.text
            assert boost.json() == {'completed': True, 'reward_days': 3}
            admin_env.boost_bot.__aexit__.assert_awaited()

            statuses = {t['id']: t['status'] for t in (await user.get('/cabinet/tasks')).json()['tasks']}
            assert statuses == {boost_id: 'completed', photo_id: 'pending'}

        from app.cabinet.routes import media as media_routes

        # Часы токенов заморожены внутри 6-часового окна: иначе граница окна между
        # двумя запросами (раз в 6 ч) сменила бы ссылку.
        monkeypatch.setattr(media_routes, 'time', SimpleNamespace(time=lambda: 1_800_001_000.0))
        queue = await admin_env.client.get('/cabinet/admin/tasks/submissions', params={'status': 'pending'})
        assert queue.status_code == 200, queue.text
        page = queue.json()
        assert page['total'] == 1
        assert page['pending_count'] == 1
        item = page['items'][0]
        assert item['task'] == {'id': photo_id, 'title': 'Лайки', 'kind': 'photo_report'}
        assert item['user'] == {
            'id': USER_ID,
            'telegram_id': 555,
            'username': 'alice',
            'full_name': 'Alice',
            'status': 'active',
        }
        assert item['comment'] == 'вот'
        media = item['media_items'][0]
        assert (media['file_id'], media['thumb_file_id']) == (FILE_A, FILE_B)

        assert media_routes._verify_media_token(FILE_A, media['token'])
        assert media_routes._verify_media_token(FILE_B, media['thumb_token'])
        # Токены стабильные (одни на всё 6-часовое окно), а не обычные «сейчас + 24 ч».
        assert (media['token'], media['thumb_token']) == (
            media_routes.make_stable_media_token(FILE_A),
            media_routes.make_stable_media_token(FILE_B),
        )
        # Ссылки на картинки не меняются при перезапросе очереди — браузер не качает превью заново.
        # Перезапрос — на 7 секунд позже в том же окне: обычный токен за это время сменился бы.
        monkeypatch.setattr(media_routes, 'time', SimpleNamespace(time=lambda: 1_800_001_007.0))
        refetched = await admin_env.client.get('/cabinet/admin/tasks/submissions', params={'status': 'pending'})
        assert refetched.json()['items'][0]['media_items'] == item['media_items']

        approved = await admin_env.client.post(
            '/cabinet/admin/tasks/submissions/approve', json={'ids': [submission['id'], 777]}
        )
        assert approved.status_code == 200, approved.text
        assert approved.json() == {
            'results': [
                {'id': submission['id'], 'ok': True, 'error_code': None},
                {'id': 777, 'ok': False, 'error_code': 'not_found'},
            ]
        }
        admin_env.notify_bot.send_message.assert_awaited_once()

        history = await admin_env.client.get('/cabinet/admin/tasks/submissions', params={'status': 'approved'})
        rows = history.json()['items']
        assert {row['source'] for row in rows} == {'cabinet', 'auto'}
        reviewed = next(row for row in rows if row['source'] == 'cabinet')
        assert reviewed['reviewer_name'] == 'Admin'
        assert reviewed['reward_days'] == 1

        rejected = await admin_env.client.post(
            '/cabinet/admin/tasks/submissions/reject', json={'ids': [submission['id']], 'reason': 'поздно'}
        )
        assert rejected.json()['results'] == [{'id': submission['id'], 'ok': False, 'error_code': 'not_pending'}]

        empty_reason = await admin_env.client.post(
            '/cabinet/admin/tasks/submissions/reject', json={'ids': [1], 'reason': '   '}
        )
        assert empty_reason.status_code == 422

        too_many_ids = await admin_env.client.post(
            '/cabinet/admin/tasks/submissions/approve', json={'ids': list(range(1, 102))}
        )
        assert too_many_ids.status_code == 422

        with_reports = await admin_env.client.delete(f'/cabinet/admin/tasks/{photo_id}')
        assert with_reports.status_code == 409
        assert with_reports.json()['detail']['code'] == 'task_has_submissions'

        kind_change = await admin_env.client.patch(
            f'/cabinet/admin/tasks/{photo_id}', json={'kind': 'channel_boost', 'channel_id': '-1003822976183'}
        )
        assert kind_change.status_code == 409

        counts = {
            t['id']: (t['pending_count'], t['approved_count'])
            for t in (await admin_env.client.get('/cabinet/admin/tasks')).json()['items']
        }
        assert counts == {photo_id: (0, 1), boost_id: (0, 1)}


@pytest.mark.asyncio
async def test_http_boost_panel_failure_is_503_with_code(monkeypatch) -> None:
    async with _client(monkeypatch, as_admin=False, panel_ok=False) as env:
        now = datetime.now(UTC)
        env.session.add(
            RewardTask(
                id=50,
                kind='channel_boost',
                title='Буст',
                reward_days=3,
                channel_id='-1003822976183',
                created_at=now,
                updated_at=now,
            )
        )
        await env.session.commit()

        response = await env.client.post('/cabinet/tasks/50/check-boost')

        assert response.status_code == 503
        assert response.json()['detail']['code'] == 'panel_sync_failed'
        # Пользователь по-прежнему видит задание доступным (ничего не записано).
        listed = await env.client.get('/cabinet/tasks')
        assert listed.json()['tasks'][0]['status'] == 'available'


def _seed_tasks(session) -> None:
    now = datetime.now(UTC)
    session.add_all(
        [
            RewardTask(id=60, kind='photo_report', title='Лайки', reward_days=1, created_at=now, updated_at=now),
            RewardTask(
                id=61,
                kind='channel_boost',
                title='Буст',
                reward_days=3,
                channel_id='-1003822976183',
                created_at=now,
                updated_at=now,
            ),
        ]
    )


# --- Hidden section (owner answers of 2026-10-09) ---------------------------


@contextlib.asynccontextmanager
async def _app_client(session, routers, user_id: int):
    """Ещё одно приложение на том же сеансе БД, с другим текущим пользователем."""
    import httpx
    from fastapi import FastAPI

    from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user

    app = FastAPI()
    for router in routers:
        app.include_router(router, prefix='/cabinet')
    app.dependency_overrides[get_cabinet_db] = lambda: session

    async def current_user():
        return await session.get(User, user_id)

    app.dependency_overrides[get_current_cabinet_user] = current_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client


async def _user_endpoints(client) -> dict:
    """Все пользовательские роуты; несуществующее задание — проверка видимости идёт первой."""
    return {
        'list': await client.get('/cabinet/tasks'),
        'submit': await client.post('/cabinet/tasks/60/submissions', json={'media': [{'file_id': FILE_A}]}),
        'boost': await client.post('/cabinet/tasks/61/check-boost'),
        'submit_unknown_task': await client.post(
            '/cabinet/tasks/999/submissions', json={'media': [{'file_id': FILE_A}]}
        ),
        'boost_unknown_task': await client.post('/cabinet/tasks/999/check-boost'),
    }


async def _assert_hidden(client, env) -> None:
    """Раздела нет: счётчик 0 (ни пункта меню, ни баннера), остальное — 404 tasks_unavailable."""
    count = await client.get('/cabinet/tasks/count')
    assert (count.status_code, count.json()) == (200, {'count': 0})
    for name, response in (await _user_endpoints(client)).items():
        assert response.status_code == 404, (name, response.text)
        assert response.json() == {'detail': UNAVAILABLE}, name
    env.boost_bot.get_user_chat_boosts.assert_not_awaited()


async def _assert_visible(client, *, eligible: bool | None = True, ineligible_reason: str | None = None) -> None:
    count = await client.get('/cabinet/tasks/count')
    assert count.json() == {'count': 2}
    listed = await client.get('/cabinet/tasks')
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert (body['eligible'], body['ineligible_reason']) == (eligible, ineligible_reason)
    assert [task['id'] for task in body['tasks']] == [60, 61]


async def _submissions(session) -> list:
    from sqlalchemy import select

    return list((await session.execute(select(RewardTaskSubmission.id))).scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize('how', ['deactivated', 'lapsed', 'reset'])
async def test_http_disabled_subscription_hides_the_section(monkeypatch, how) -> None:
    """Любая DISABLED («Деактивировать» с остатком срока, кончившаяся, «Обнулить») — раздела нет.

    До 2026-10-09 здесь было 403 subscription_disabled с объяснением, а кончившаяся DISABLED оживала.
    """
    from sqlalchemy import update

    from app.database.crud.subscription import reset_subscription

    async with _client(monkeypatch, as_admin=False) as env:
        _seed_tasks(env.session)
        if how == 'reset':
            await reset_subscription(env.session, await env.session.get(Subscription, 10))
        else:
            values = {'status': 'disabled'}
            if how == 'lapsed':
                values['end_date'] = datetime.now(UTC) - timedelta(days=5)
            await env.session.execute(update(Subscription).where(Subscription.id == 10).values(**values))
        await env.session.commit()
        env.session.expire_all()
        before = (await env.session.get(Subscription, 10)).end_date

        await _assert_hidden(env.client, env)

        env.session.expire_all()
        subscription = await env.session.get(Subscription, 10)
        assert (subscription.status, subscription.end_date) == ('disabled', before)
        assert await _submissions(env.session) == []


@pytest.mark.asyncio
async def test_http_restricted_and_no_subscription_still_see_why(monkeypatch) -> None:
    """Без подписки и с ограничением от админа раздел виден — с объяснением (как раньше)."""
    from sqlalchemy import delete

    async with _client(monkeypatch, as_admin=False) as env:
        _seed_tasks(env.session)
        user = await env.session.get(User, USER_ID)
        user.restriction_subscription = True
        await env.session.commit()
        await _assert_visible(env.client, eligible=False, ineligible_reason='restricted')
        boosted = await env.client.post('/cabinet/tasks/61/check-boost')
        assert (boosted.status_code, boosted.json()['detail']['code']) == (403, 'restricted')

        user = await env.session.get(User, USER_ID)
        user.restriction_subscription = False
        await env.session.execute(delete(Subscription))
        await env.session.commit()
        env.session.expire_all()
        await _assert_visible(env.client, eligible=False, ineligible_reason='no_subscription')
        submitted = await env.client.post('/cabinet/tasks/60/submissions', json={'media': [{'file_id': FILE_A}]})
        assert (submitted.status_code, submitted.json()['detail']['code']) == (400, 'no_subscription')


@pytest.mark.asyncio
async def test_http_admins_only_mode(monkeypatch) -> None:
    """REWARD_TASKS_FOR_ALL_USERS выключен: раздел видят только админы.

    Админ — тот, кто открывает админку кабинета: ADMIN_IDS / ADMIN_EMAILS или активная
    роль уровня выше 0. Роль уровня 0, истёкшая или выключенная — не админ.
    """
    from app.cabinet.routes import reward_tasks as user_routes

    monkeypatch.setattr(settings, SWITCH, False)
    monkeypatch.setattr(settings, 'ADMIN_IDS', str(ADMIN_TELEGRAM_ID))
    monkeypatch.setattr(settings, 'ADMIN_EMAILS', '')
    async with _client(monkeypatch, as_admin=False) as env:
        _seed_tasks(env.session)
        await env.session.commit()

        # Обычный пользователь — раздела нет.
        await _assert_hidden(env.client, env)

        # Админ из ADMIN_IDS видит раздел (подписки у него нет — объяснение).
        async with _app_client(env.session, [user_routes.router], ADMIN_ID) as admin:
            await _assert_visible(admin, eligible=False, ineligible_reason='no_subscription')

        # Роли кабинета: только активная, не истёкшая, уровня выше 0.
        now = datetime.now(UTC)
        env.session.add_all(
            [
                AdminRole(id=1, name='Viewer', level=0, permissions=[]),
                AdminRole(id=2, name='Marketer', level=30, permissions=['tasks:*']),
                AdminRole(id=3, name='Old', level=50, permissions=['*:*'], is_active=False),
            ]
        )
        marketer = UserRole(user_id=USER_ID, role_id=2, is_active=True, expires_at=now - timedelta(days=1))
        env.session.add_all(
            [UserRole(user_id=USER_ID, role_id=1, is_active=True), UserRole(user_id=USER_ID, role_id=3, is_active=True)]
        )
        env.session.add(marketer)
        await env.session.commit()
        await _assert_hidden(env.client, env)

        marketer.expires_at = now + timedelta(days=1)
        await env.session.commit()
        await _assert_visible(env.client)
        submitted = await env.client.post('/cabinet/tasks/60/submissions', json={'media': [{'file_id': FILE_A}]})
        assert submitted.status_code == 201, submitted.text


@pytest.mark.asyncio
async def test_http_switch_toggled_in_cabinet_settings_takes_effect_at_once(monkeypatch) -> None:
    """Настоящий роут админ-настроек кабинета меняет переключатель — без перезапуска и кэша.

    Счётчик (пункт меню и баннер) и список меняются со следующего же запроса.
    """
    from sqlalchemy import select

    from app.cabinet.routes.admin_settings import router as settings_router
    from app.services.system_settings_service import BotConfigurationService

    monkeypatch.setattr(settings, SWITCH, False)
    monkeypatch.setattr(BotConfigurationService, '_overrides_raw', {})
    async with _client(monkeypatch, as_admin=False) as env:
        _seed_tasks(env.session)
        await env.session.commit()
        await _assert_hidden(env.client, env)

        async with _app_client(env.session, [settings_router], ADMIN_ID) as admin:
            listed = await admin.get('/cabinet/admin/settings', params={'category_key': 'REWARD_TASKS'})
            assert listed.status_code == 200, listed.text
            [definition] = listed.json()
            assert definition['key'] == SWITCH
            assert definition['category'] == {'key': 'REWARD_TASKS', 'label': '🎁 Задания'}
            assert (definition['type'], definition['current'], definition['original']) == ('bool', False, False)
            assert (definition['env_locked'], definition['read_only']) == (False, False)
            assert 'Задания' in definition['hint']['description']

            switched_on = await admin.put(f'/cabinet/admin/settings/{SWITCH}', json={'value': True})
            assert switched_on.status_code == 200, switched_on.text
            assert switched_on.json()['current'] is True
            assert settings.REWARD_TASKS_FOR_ALL_USERS is True
            stored = await env.session.execute(select(SystemSetting.value).where(SystemSetting.key == SWITCH))
            assert stored.scalar_one() == 'true'

            await _assert_visible(env.client)

            switched_off = await admin.put(f'/cabinet/admin/settings/{SWITCH}', json={'value': False})
            assert switched_off.json()['current'] is False
            await _assert_hidden(env.client, env)

            await admin.put(f'/cabinet/admin/settings/{SWITCH}', json={'value': True})
            await _assert_visible(env.client)
            reset = await admin.delete(f'/cabinet/admin/settings/{SWITCH}')
            assert reset.json()['current'] is False  # по умолчанию выключен
            await _assert_hidden(env.client, env)


@pytest.mark.asyncio
async def test_http_admin_still_approves_with_subscription_disabled_code(monkeypatch) -> None:
    """Админка не меняется: отчёт, чья подписка с тех пор стала DISABLED, — subscription_disabled."""
    from sqlalchemy import update

    async with _client(monkeypatch, as_admin=True) as env:
        _seed_tasks(env.session)
        env.session.add(
            RewardTaskSubmission(
                id=71,
                task_id=60,
                user_id=USER_ID,
                status='pending',
                source='cabinet',
                media_items=[{'type': 'photo', 'file_id': FILE_A}],
                created_at=datetime.now(UTC),
            )
        )
        await env.session.execute(update(Subscription).where(Subscription.id == 10).values(status='disabled'))
        await env.session.commit()

        approved = await env.client.post('/cabinet/admin/tasks/submissions/approve', json={'ids': [71]})

        assert approved.json() == {'results': [{'id': 71, 'ok': False, 'error_code': 'subscription_disabled'}]}
        still_pending = await env.client.get('/cabinet/admin/tasks/submissions', params={'status': 'pending'})
        assert [item['id'] for item in still_pending.json()['items']] == [71]
        env.notify_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_admin_sees_blocked_author_and_cannot_approve(monkeypatch) -> None:
    from sqlalchemy import update

    async with _client(monkeypatch, as_admin=True) as env:
        _seed_tasks(env.session)
        env.session.add(
            RewardTaskSubmission(
                id=70,
                task_id=60,
                user_id=USER_ID,
                status='pending',
                source='bot',
                media_items=[{'type': 'photo', 'file_id': FILE_A, 'thumb_file_id': FILE_B}],
                created_at=datetime.now(UTC),
            )
        )
        await env.session.execute(update(User).where(User.id == USER_ID).values(status='blocked'))
        await env.session.commit()

        queue = await env.client.get('/cabinet/admin/tasks/submissions', params={'status': 'pending'})
        assert queue.status_code == 200, queue.text
        assert queue.json()['items'][0]['user']['status'] == 'blocked'

        approved = await env.client.post('/cabinet/admin/tasks/submissions/approve', json={'ids': [70]})
        assert approved.json() == {'results': [{'id': 70, 'ok': False, 'error_code': 'user_blocked'}]}
        still_pending = await env.client.get('/cabinet/admin/tasks/submissions', params={'status': 'pending'})
        assert [item['id'] for item in still_pending.json()['items']] == [70]

        rejected = await env.client.post(
            '/cabinet/admin/tasks/submissions/reject', json={'ids': [70], 'reason': 'Аккаунт заблокирован'}
        )
        assert rejected.json() == {'results': [{'id': 70, 'ok': True, 'error_code': None}]}
        env.notify_bot.send_message.assert_not_awaited()
