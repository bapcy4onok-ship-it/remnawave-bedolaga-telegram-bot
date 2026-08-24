"""Кабинет отдаёт боту ClientID Метрики вместе с запросом авторизации — он должен доезжать до БД.

Фронт (`src/api/auth.ts`) кладёт `yandex_cid` в тело всех запросов входа/регистрации,
а схемы бота этого поля не знали: pydantic по умолчанию `extra='ignore'`, поэтому CID
молча выбрасывался, строка в `yandex_client_id_map` не появлялась и офлайн-конверсия
`registration` для кабинетных регистраций не могла уйти в принципе.

Тесты пинят четыре вещи:
  * поле принимается схемами (и мусор отсекается на границе);
  * при успешном создании аккаунта CID реально доезжает до `upsert_cid`;
  * повторный вход существующего пользователя строку НЕ трогает;
  * любая ошибка записи не ломает авторизацию, а CID не утекает в логи целиком.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes import auth as auth_routes
from app.cabinet.routes.auth import (
    auth_telegram,
    auth_telegram_widget,
    register_email_standalone,
)
from app.cabinet.routes.oauth import OAuthCallbackRequest, _finalize_oauth_login
from app.cabinet.schemas.auth import (
    EmailLoginRequest,
    EmailRegisterRequest,
    EmailRegisterStandaloneRequest,
    TelegramAuthRequest,
    TelegramOIDCAuthRequest,
    TelegramWidgetAuthRequest,
)
from app.database.models import UserStatus


VALID_CID = '1712345678901234567'
MASKED_CID = '*' * (len(VALID_CID) - 4) + VALID_CID[-4:]
TG_ID = 555_000_111
HASH = 'a' * 64

SERVICE = 'app.services.yandex_offline_conv_service'
CAPTURE = f'{SERVICE}.store_cid_and_fire_registration'


# --------------------------------------------------------------------------------------
# Схемы
# --------------------------------------------------------------------------------------

SCHEMA_CASES = [
    (TelegramAuthRequest, {'init_data': 'query_id=x'}),
    (TelegramWidgetAuthRequest, {'id': TG_ID, 'first_name': 'A', 'auth_date': 1700000000, 'hash': HASH}),
    (TelegramOIDCAuthRequest, {'id_token': 'jwt'}),
    (EmailRegisterRequest, {'email': 'a@example.com', 'password': 'password123'}),
    (EmailLoginRequest, {'email': 'a@example.com', 'password': 'password123'}),
    (EmailRegisterStandaloneRequest, {'email': 'a@example.com', 'password': 'password123'}),
    (OAuthCallbackRequest, {'code': 'c', 'state': 's'}),
]


@pytest.mark.parametrize('model, kwargs', SCHEMA_CASES)
def test_auth_schemas_accept_yandex_cid(model, kwargs) -> None:
    """Поле принимается каждой схемой, на которую фронт шлёт CID, и опционально."""
    assert model(**kwargs, yandex_cid=VALID_CID).yandex_cid == VALID_CID
    assert model(**kwargs).yandex_cid is None


@pytest.mark.parametrize('model, kwargs', SCHEMA_CASES)
@pytest.mark.parametrize(
    'bad',
    [
        '',  # пустая строка
        '   ',  # пробелы
        'abc',  # короче четырёх символов
        'кириллица',
        'a/b+c',
        "1'; DROP TABLE users; --",
        'x' * 300,  # длиннее varchar(128)
        '_subid_only',  # зарезервированный префикс плейсхолдера
        '_1712345678901234567',
        12345,  # вообще не строка
        ['1712345678901234567'],
    ],
)
def test_malformed_yandex_cid_is_ignored_never_rejected(model, kwargs, bad) -> None:
    """Мусор в аналитическом поле обязан гаситься в None, а НЕ давать 422.

    Это эндпоинты входа. pattern/max_length на поле означали бы отказ авторизации
    из-за аналитики, причём залипающий: CID живёт в localStorage браузера и уехал бы
    в каждой следующей попытке входа — человек не смог бы зайти, пока не почистит storage.
    """
    assert model(**kwargs, yandex_cid=bad).yandex_cid is None


@pytest.mark.parametrize('model, kwargs', SCHEMA_CASES)
def test_yandex_cid_is_trimmed(model, kwargs) -> None:
    assert model(**kwargs, yandex_cid=f'  {VALID_CID}\n').yandex_cid == VALID_CID


@pytest.mark.parametrize('model, kwargs', SCHEMA_CASES)
def test_unknown_fields_still_ignored(model, kwargs) -> None:
    """Старые/новые клиенты со «своими» полями по-прежнему не получают 422."""
    assert model(**kwargs, some_future_field='x').yandex_cid is None


def test_reserved_placeholder_prefix_cannot_come_from_client() -> None:
    """'_' в начале — пространство плейсхолдеров upsert_subid ('_subid_only').

    Сервис конверсий считает такой CID «настоящего ещё нет» и навсегда глушит для
    юзера registration/trial/purchase (см. on_registration/on_trial/on_purchase),
    поэтому клиент не должен уметь записать себе такое значение.
    """
    from app.cabinet.schemas.auth import _sanitize_yandex_cid
    from app.services.yandex_offline_conv_service import _normalize_cid

    assert _normalize_cid('_subid_only') == '_subid_only', 'сервис такой CID пропускает — гейт обязан быть в схеме'
    assert _sanitize_yandex_cid('_subid_only') is None
    assert _sanitize_yandex_cid(f'_{VALID_CID}') is None


def test_sanitized_cid_fits_column_and_service_normalizer() -> None:
    """Что прошло схему — обязано пройти `_normalize_cid` и влезть в varchar(128)."""
    from app.cabinet.schemas.auth import _sanitize_yandex_cid
    from app.database.models import YandexClientIdMap
    from app.services.yandex_offline_conv_service import _normalize_cid

    column_limit = YandexClientIdMap.__table__.c.yandex_cid.type.length
    for raw in (VALID_CID, 'a' * 128, 'A-b.c:d', '1234'):
        sanitized = _sanitize_yandex_cid(raw)
        assert sanitized is not None, raw
        assert len(sanitized) <= column_limit
        assert _normalize_cid(sanitized) == sanitized


GARBAGE_CIDS = ['', '   ', 'abc', 'кириллица', "1'; DROP TABLE users; --", 'x' * 300, '_subid_only', 12345, None]


def test_login_endpoint_never_answers_422_because_of_yandex_cid() -> None:
    """Сквозная проверка на HTTP: неверный init_data — это 401, что бы ни лежало в yandex_cid.

    422 здесь означал бы отказ авторизации из-за аналитического поля (и залипающий:
    значение живёт в localStorage браузера), поэтому статус пинится на уровне запроса,
    а не только на уровне схемы.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.cabinet.dependencies import get_cabinet_db
    from app.cabinet.routes.auth import router

    app = FastAPI()
    app.include_router(router, prefix='/cabinet')
    app.dependency_overrides[get_cabinet_db] = lambda: AsyncMock()

    bad_init_data = 'query_id=AAAA&user=%7B%22id%22%3A1%7D&auth_date=1700000000&hash=deadbeef'
    bodies = [{'init_data': bad_init_data}]  # старый клиент: поля нет вовсе
    bodies += [{'init_data': bad_init_data, 'yandex_cid': cid} for cid in GARBAGE_CIDS]
    bodies.append({'init_data': bad_init_data, 'yandex_cid': VALID_CID, 'some_future_field': 'x'})

    with (
        patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        for body in bodies:
            response = client.post('/cabinet/auth/telegram', json=body)
            assert response.status_code == 401, f'{body.get("yandex_cid")!r} -> {response.status_code}'


def test_capture_sources_fit_column() -> None:
    """`yandex_client_id_map.source` — String(20): длинный литерал уронил бы INSERT."""
    import re

    for path in ('app/cabinet/routes/auth.py', 'app/cabinet/routes/oauth.py'):
        source_file = Path(__file__).resolve().parents[2] / path
        for value in re.findall(r"_capture_yandex_cid\([^)]*source='([^']+)'", source_file.read_text(encoding='utf-8')):
            assert len(value) <= 20, f'{path}: source={value!r} длиннее varchar(20)'


# --------------------------------------------------------------------------------------
# Инфраструктура для вызова эндпоинтов «на моках»
# --------------------------------------------------------------------------------------


def _user(**kw) -> SimpleNamespace:
    base = {
        'id': 42,
        'telegram_id': TG_ID,
        'username': 'nick',
        'first_name': 'F',
        'last_name': 'L',
        'status': UserStatus.ACTIVE.value,
        'cabinet_last_login': None,
        'email': None,
        'email_verified': False,
        'email_verified_at': None,
        'email_verification_token': None,
        'email_verification_expires': None,
        'language': 'ru',
        'pending_campaign_slug': None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _auth_response() -> SimpleNamespace:
    return SimpleNamespace(refresh_token='refresh', campaign_bonus=None, user=None)


class _FakeSession:
    """Заглушка `AsyncSessionLocal()` сервиса конверсий."""

    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.flush = AsyncMock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


@contextmanager
def _telegram_patches(*, existing_user=None, created_user=None, extra=()):
    """Мокаем всё, что нужно обоим telegram-эндпоинтам, кроме записи CID."""
    with ExitStack() as stack:
        add = stack.enter_context
        add(patch('app.cabinet.routes.auth.get_client_ip', MagicMock(return_value='1.2.3.4')))
        add(patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)))
        add(patch('app.cabinet.routes.auth.TokenReplayCache.is_token_replayed', AsyncMock(return_value=False)))
        add(patch('app.cabinet.routes.auth.validate_telegram_login_widget', MagicMock(return_value=True)))
        add(
            patch(
                'app.cabinet.routes.auth.validate_telegram_init_data',
                MagicMock(return_value={'id': TG_ID, 'first_name': 'F'}),
            )
        )
        add(patch('app.cabinet.routes.auth.get_user_by_telegram_id', AsyncMock(return_value=existing_user)))
        add(patch('app.cabinet.routes.auth.create_user', AsyncMock(return_value=created_user)))
        add(patch('app.cabinet.routes.auth._require_legal_consent', AsyncMock(return_value=[])))
        add(patch('app.cabinet.routes.auth.legal_consent_service.record_consent', AsyncMock()))
        add(
            patch(
                'app.cabinet.routes.auth._create_auth_response', AsyncMock(side_effect=lambda *a, **k: _auth_response())
            )
        )
        add(patch('app.cabinet.routes.auth._store_refresh_token', AsyncMock()))
        add(patch('app.cabinet.routes.auth._process_referral_code', AsyncMock()))
        add(patch('app.cabinet.routes.auth._process_campaign_bonus', AsyncMock(return_value=None)))
        add(patch('app.services.referral_service.get_pending_referral', AsyncMock(return_value=None)))
        add(patch('app.services.referral_service.attach_referrer_if_missing', AsyncMock()))
        for extra_patch in extra:
            add(extra_patch)
        yield


@contextmanager
def _db_level_patches():
    """Подменяем только «дно» сервиса — сессию и `upsert_cid`, остальное настоящее."""
    upsert = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(patch(f'{SERVICE}.AsyncSessionLocal', MagicMock(side_effect=_FakeSession)))
        stack.enter_context(patch(f'{SERVICE}.upsert_cid', upsert))
        stack.enter_context(patch(f'{SERVICE}.spawn_bg', MagicMock(side_effect=lambda coro: coro.close())))
        yield upsert


# --------------------------------------------------------------------------------------
# Ловушка HMAC: любое лишнее поле в data-check-string ломает подпись Telegram
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_widget_hmac_payload_excludes_yandex_cid() -> None:
    """`yandex_cid` (и галочки документов) не должны попадать в data-check-string.

    validate_telegram_login_widget собирает строку из ВСЕХ не-None полей дампа:
    наше поле внутри неё означало бы 401 на каждом виджет-логине с закэшированным CID.
    """
    request = TelegramWidgetAuthRequest(
        id=TG_ID,
        first_name='A',
        auth_date=1700000000,
        hash=HASH,
        yandex_cid=VALID_CID,
        accepted_legal_documents=['terms'],
    )
    validate = MagicMock(return_value=False)  # дальше по коду — 401, нам нужен только payload
    with ExitStack() as stack:
        stack.enter_context(patch('app.cabinet.routes.auth.get_client_ip', MagicMock(return_value='1.2.3.4')))
        stack.enter_context(
            patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False))
        )
        stack.enter_context(patch('app.cabinet.routes.auth.validate_telegram_login_widget', validate))
        with pytest.raises(HTTPException):
            await auth_telegram_widget(request=request, raw_request=MagicMock(), db=AsyncMock())

    payload = validate.call_args.args[0]
    assert 'yandex_cid' not in payload, 'CID в data-check-string ломает HMAC → 401 на каждом виджет-логине'
    assert 'accepted_legal_documents' not in payload


# --------------------------------------------------------------------------------------
# Запись CID
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_widget_new_user_persists_cid_to_db() -> None:
    """Новый аккаунт через Login Widget: CID доезжает до `upsert_cid` тем же путём, что и branding."""
    created = _user()
    request = TelegramWidgetAuthRequest(id=TG_ID, first_name='A', auth_date=1700000000, hash=HASH, yandex_cid=VALID_CID)

    with _db_level_patches() as upsert, _telegram_patches(existing_user=None, created_user=created):
        response = await auth_telegram_widget(request=request, raw_request=MagicMock(), db=AsyncMock())

    assert response is not None
    upsert.assert_awaited_once()
    args, kwargs = upsert.await_args
    assert args[1] == created.id
    assert args[2] == VALID_CID
    assert kwargs['source'] == 'cabinet_widget'


@pytest.mark.asyncio
async def test_telegram_initdata_new_user_captures_cid() -> None:
    """initData-регистрация пишет CID со своим `source`."""
    created = _user()
    request = TelegramAuthRequest(init_data='query_id=x', yandex_cid=VALID_CID)
    capture = AsyncMock()

    with _telegram_patches(existing_user=None, created_user=created, extra=[patch(CAPTURE, capture)]):
        await auth_telegram(request=request, raw_request=MagicMock(), db=AsyncMock())

    capture.assert_awaited_once_with(created.id, VALID_CID, source='cabinet_telegram')


@pytest.mark.asyncio
async def test_returning_user_login_does_not_touch_stored_cid() -> None:
    """Повторный вход: строку существующего пользователя не переписываем.

    `upsert_cid` перезаписывает CID безусловно и истории не хранит, а `registration` —
    first-touch-цель. Перезапись на логине подменила бы CID визита приобретения
    на CID случайного более позднего браузера.
    """
    existing = _user()
    request = TelegramWidgetAuthRequest(
        id=TG_ID, first_name='A', auth_date=1700000000, hash=HASH, yandex_cid='9999999999999999999'
    )
    capture = AsyncMock()

    with _telegram_patches(existing_user=existing, extra=[patch(CAPTURE, capture)]):
        await auth_telegram_widget(request=request, raw_request=MagicMock(), db=AsyncMock())

    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_cid_never_reaches_db() -> None:
    """Мусорный CID, проскочивший мимо схемы, отсекается нормализацией сервиса."""
    created = _user()
    # model_construct обходит валидацию — имитируем «поле пришло, но кривое».
    request = TelegramWidgetAuthRequest.model_construct(
        id=TG_ID,
        first_name='A',
        last_name=None,
        username=None,
        photo_url=None,
        auth_date=1700000000,
        hash=HASH,
        campaign_slug=None,
        referral_code=None,
        accepted_legal_documents=None,
        yandex_cid='../../etc/passwd',
    )

    with _db_level_patches() as upsert, _telegram_patches(existing_user=None, created_user=created):
        response = await auth_telegram_widget(request=request, raw_request=MagicMock(), db=AsyncMock())

    assert response is not None, 'кривой CID не должен ломать вход'
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_survives_cid_write_failure_and_masks_cid_in_log() -> None:
    """Аналитика не имеет права ломать вход, а CID не должен попадать в лог целиком."""
    created = _user()
    request = TelegramWidgetAuthRequest(id=TG_ID, first_name='A', auth_date=1700000000, hash=HASH, yandex_cid=VALID_CID)
    exploding = AsyncMock(side_effect=RuntimeError('db is down'))
    debug = MagicMock()

    with (
        _telegram_patches(
            existing_user=None,
            created_user=created,
            extra=[patch(CAPTURE, exploding), patch.object(auth_routes.logger, 'debug', debug)],
        ),
    ):
        response = await auth_telegram_widget(request=request, raw_request=MagicMock(), db=AsyncMock())

    assert response is not None, 'падение записи CID обязано быть проглочено'
    exploding.assert_awaited_once()
    logged = debug.call_args.kwargs
    assert logged['cid'] == MASKED_CID
    assert VALID_CID not in str(debug.call_args), 'CID в логах должен быть замаскирован'


@pytest.mark.asyncio
async def test_slow_cid_write_cannot_stall_login() -> None:
    """Зависшая запись CID не имеет права держать вход.

    Строку юзера может держать чужая транзакция (бот на /start, админка), а FK
    `yandex_client_id_map.user_id -> users` заставляет INSERT брать FOR KEY SHARE,
    который конфликтует с FOR UPDATE. Без потолка ожидание упирается в asyncpg
    command_timeout (30 c) — для человека это «регистрация не прошла».
    """
    created = _user()
    request = TelegramWidgetAuthRequest(id=TG_ID, first_name='A', auth_date=1700000000, hash=HASH, yandex_cid=VALID_CID)

    async def _hangs(*_args, **_kwargs) -> None:
        await asyncio.sleep(30)

    started = time.perf_counter()
    with _telegram_patches(
        existing_user=None,
        created_user=created,
        extra=[patch(CAPTURE, _hangs), patch.object(auth_routes, '_YANDEX_CID_CAPTURE_TIMEOUT', 0.05)],
    ):
        response = await auth_telegram_widget(request=request, raw_request=MagicMock(), db=AsyncMock())
    elapsed = time.perf_counter() - started

    assert response is not None, 'вход обязан состояться даже при зависшей записи CID'
    assert elapsed < 5, f'вход ждал запись CID {elapsed:.1f} c'


def test_capture_timeout_is_sane() -> None:
    assert 0 < auth_routes._YANDEX_CID_CAPTURE_TIMEOUT <= 10


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'endpoint, request_factory',
    [
        (auth_telegram, lambda: TelegramAuthRequest(init_data='query_id=x', yandex_cid=VALID_CID)),
        (
            auth_telegram_widget,
            lambda: TelegramWidgetAuthRequest(
                id=TG_ID, first_name='A', auth_date=1700000000, hash=HASH, yandex_cid=VALID_CID
            ),
        ),
    ],
)
async def test_cid_is_written_before_the_row_locking_block(endpoint, request_factory) -> None:
    """Запись CID должна идти ДО _process_referral_code/_process_campaign_bonus.

    Оба берут `SELECT ... FOR UPDATE` по строке юзера и на ряде веток (bonus_type='none',
    «бонус уже начислен», ранние return) не коммитят. Запись CID идёт в ОТДЕЛЬНОЙ сессии,
    а FK на users заставляет её брать FOR KEY SHARE, который с FOR UPDATE конфликтует:
    вторая сессия встала бы ждать транзакцию, закоммитить которую может только эта же
    корутина после возврата из await. PostgreSQL такой self-deadlock не видит — запрос
    висит до таймаута. Порядок здесь — не косметика.
    """
    order: list[str] = []
    capture = AsyncMock(side_effect=lambda *a, **k: order.append('cid'))
    referral = AsyncMock(side_effect=lambda *a, **k: order.append('referral'))
    campaign = AsyncMock(return_value=None, side_effect=lambda *a, **k: order.append('campaign'))

    with _telegram_patches(
        existing_user=None,
        created_user=_user(),
        extra=[
            patch(CAPTURE, capture),
            patch('app.cabinet.routes.auth._process_referral_code', referral),
            patch('app.cabinet.routes.auth._process_campaign_bonus', campaign),
        ],
    ):
        await endpoint(request=request_factory(), raw_request=MagicMock(), db=AsyncMock())

    assert order[0] == 'cid', f'CID пишется после блокировок строки юзера: {order}'
    assert {'referral', 'campaign'} <= set(order)


@pytest.mark.asyncio
async def test_oauth_writes_cid_before_the_row_locking_block() -> None:
    """То же требование к порядку в OAuth-колбэке."""
    order: list[str] = []
    capture = AsyncMock(side_effect=lambda *a, **k: order.append('cid'))
    referral = AsyncMock(side_effect=lambda *a, **k: order.append('referral'))
    campaign = AsyncMock(return_value=None, side_effect=lambda *a, **k: order.append('campaign'))

    with ExitStack() as stack:
        add = stack.enter_context
        add(
            patch(
                'app.cabinet.routes.oauth._create_auth_response',
                AsyncMock(side_effect=lambda *a, **k: _auth_response()),
            )
        )
        add(patch('app.cabinet.routes.oauth._store_refresh_token', AsyncMock()))
        add(patch('app.cabinet.routes.oauth._process_campaign_bonus', campaign))
        add(patch('app.cabinet.routes.auth._process_referral_code', referral))
        add(patch(CAPTURE, capture))
        await _finalize_oauth_login(AsyncMock(), _user(), 'yandex', None, None, is_new_user=True, yandex_cid=VALID_CID)

    assert order[0] == 'cid', f'CID пишется после блокировок строки юзера: {order}'
    assert {'referral', 'campaign'} <= set(order)


@pytest.mark.asyncio
async def test_standalone_email_registration_captures_cid() -> None:
    """Регистрация по почте: аккаунт создаётся всегда, CID пишем без флага is_new_user."""
    created = _user(email='newbie@example.com', telegram_id=None)
    request = EmailRegisterStandaloneRequest(email='newbie@example.com', password='password123', yandex_cid=VALID_CID)
    capture = AsyncMock()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

    with ExitStack() as stack:
        add = stack.enter_context
        add(patch('app.cabinet.routes.auth.get_client_ip', MagicMock(return_value='1.2.3.4')))
        add(patch('app.cabinet.routes.auth.RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)))
        add(patch('app.cabinet.routes.auth.disposable_email_service.is_disposable', MagicMock(return_value=False)))
        add(patch('app.cabinet.routes.auth.hash_password', MagicMock(return_value='hash')))
        add(patch('app.cabinet.routes.auth._require_legal_consent', AsyncMock(return_value=[])))
        add(patch('app.cabinet.routes.auth.create_user_by_email', AsyncMock(return_value=created)))
        add(patch('app.cabinet.routes.auth.legal_consent_service.record_consent', AsyncMock()))
        add(patch('app.cabinet.routes.auth._sync_subscription_from_panel_by_email', AsyncMock()))
        add(patch('app.cabinet.routes.auth.email_service', MagicMock(is_configured=MagicMock(return_value=False))))
        add(patch('app.cabinet.routes.auth._process_campaign_bonus', AsyncMock(return_value=None)))
        add(patch(CAPTURE, capture))
        response = await register_email_standalone(request=request, raw_request=MagicMock(), db=db)

    assert response.email == 'newbie@example.com'
    capture.assert_awaited_once_with(created.id, VALID_CID, source='cabinet_email')


@pytest.mark.asyncio
@pytest.mark.parametrize('is_new_user, expected_calls', [(True, 1), (False, 0)])
async def test_oauth_callback_captures_cid_only_for_new_accounts(is_new_user, expected_calls) -> None:
    """OAuth-колбэк ведёт себя так же: пишем только только что созданный аккаунт."""
    user = _user()
    capture = AsyncMock()

    with ExitStack() as stack:
        add = stack.enter_context
        add(
            patch(
                'app.cabinet.routes.oauth._create_auth_response',
                AsyncMock(side_effect=lambda *a, **k: _auth_response()),
            )
        )
        add(patch('app.cabinet.routes.oauth._store_refresh_token', AsyncMock()))
        add(patch('app.cabinet.routes.oauth._process_campaign_bonus', AsyncMock(return_value=None)))
        add(patch('app.cabinet.routes.auth._process_referral_code', AsyncMock()))
        add(patch(CAPTURE, capture))
        await _finalize_oauth_login(
            AsyncMock(),
            user,
            'yandex',
            None,
            None,
            is_new_user=is_new_user,
            yandex_cid=VALID_CID,
        )

    assert capture.await_count == expected_calls
    if expected_calls:
        assert capture.await_args == ((user.id, VALID_CID), {'source': 'cabinet_oauth'})
