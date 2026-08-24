"""Authentication schemas for cabinet."""

import re
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, EmailStr, Field


# Кэшированный ClientID Метрики, который фронт кладёт в тело КАЖДОГО запроса входа
# и регистрации (src/api/auth.ts, src/api/oauth.ts). См. PurchasePreviewRequest.yandex_cid (#558449).
#
# Поле аналитическое, а эндпоинты — входные, поэтому НИКАКИХ pattern/max_length:
# констрейнт pydantic превратил бы кривое значение в 422, то есть в отказ авторизации.
# Значение живёт в localStorage (ключ ym_client_id) и пишется туда «как есть» из
# ym('getClientID'), так что один испорченный ClientID залипал бы намертво: браузер
# слал бы его при каждой попытке входа и человек не смог бы зайти в кабинет, пока не
# почистит storage. Поэтому мусор молча превращается в None и до БД просто не доезжает.
#
# Символ '_' в начале исключён сознательно: в yandex_client_id_map это зарезервированное
# пространство плейсхолдеров (upsert_subid пишет '_subid_only'), а сервис конверсий
# трактует любой CID с таким префиксом как «настоящего CID ещё нет» и навсегда глушит
# для юзера registration/trial/purchase. Клиент не должен уметь занять это пространство.
_YANDEX_CID_RE = re.compile(r'^[A-Za-z0-9.:-][A-Za-z0-9._:-]{3,127}$')


def _sanitize_yandex_cid(value: Any) -> str | None:
    """Привести значение к валидному CID или к None. Никогда не бросает — см. комментарий выше."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _YANDEX_CID_RE.match(value):
        return None
    return value


YandexCid = Annotated[str | None, BeforeValidator(_sanitize_yandex_cid)]

YANDEX_CID_DESCRIPTION = 'Cached Yandex.Metrika ClientID (optional). Malformed values are ignored, never rejected.'


class TelegramAuthRequest(BaseModel):
    """Request for Telegram WebApp initData authentication."""

    init_data: str = Field(..., max_length=4096, description='Telegram WebApp initData string')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    referral_code: str | None = Field(
        None, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$', description='Referral code of inviter'
    )
    accepted_legal_documents: list[str] | None = Field(
        None,
        max_length=8,
        description=(
            'Ключи документов, с которыми пользователь согласился на экране первой авторизации '
            '(см. GET /cabinet/info/legal-consent). Нужны только при создании НОВОГО аккаунта; '
            'для существующего игнорируются.'
        ),
    )
    yandex_cid: YandexCid = Field(None, description=YANDEX_CID_DESCRIPTION)  # см. YandexCid выше


class TelegramWidgetAuthRequest(BaseModel):
    """Request for Telegram Login Widget authentication."""

    id: int = Field(..., description='Telegram user ID')
    first_name: str = Field(..., max_length=64, description="User's first name")
    last_name: str | None = Field(None, max_length=64, description="User's last name")
    username: str | None = Field(None, max_length=32, description="User's username")
    photo_url: str | None = Field(None, max_length=512, description="User's photo URL")
    auth_date: int = Field(..., description='Unix timestamp of authentication')
    hash: str = Field(..., min_length=64, max_length=64, description='Authentication hash')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    referral_code: str | None = Field(
        None, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$', description='Referral code of inviter'
    )
    accepted_legal_documents: list[str] | None = Field(
        None,
        max_length=8,
        description=(
            'Ключи документов, с которыми пользователь согласился на экране первой авторизации '
            '(см. GET /cabinet/info/legal-consent). Нужны только при создании НОВОГО аккаунта; '
            'для существующего игнорируются.'
        ),
    )
    yandex_cid: YandexCid = Field(None, description=YANDEX_CID_DESCRIPTION)  # см. YandexCid выше


class TelegramOIDCAuthRequest(BaseModel):
    """Request for Telegram OIDC authentication (popup flow)."""

    id_token: str = Field(..., max_length=4096, description='JWT id_token from Telegram OIDC popup')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    referral_code: str | None = Field(
        None, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$', description='Referral code of inviter'
    )
    accepted_legal_documents: list[str] | None = Field(
        None,
        max_length=8,
        description=(
            'Ключи документов, с которыми пользователь согласился на экране первой авторизации '
            '(см. GET /cabinet/info/legal-consent). Нужны только при создании НОВОГО аккаунта; '
            'для существующего игнорируются.'
        ),
    )
    yandex_cid: YandexCid = Field(None, description=YANDEX_CID_DESCRIPTION)  # см. YandexCid выше


class EmailRegisterRequest(BaseModel):
    """Request to register/link email to existing Telegram account."""

    email: EmailStr = Field(..., description='Email address')
    password: str = Field(..., min_length=8, max_length=128, description='Password (min 8 chars)')
    # Принимается только ради контракта: это вход/линковка существующего аккаунта,
    # а CID возвращающегося пользователя мы не переписываем (см. _capture_yandex_cid).
    yandex_cid: YandexCid = Field(None, description=YANDEX_CID_DESCRIPTION)


class EmailVerifyRequest(BaseModel):
    """Request to verify email with token."""

    token: str = Field(..., max_length=2048, description='Email verification token')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )


class EmailLoginRequest(BaseModel):
    """Request to login with email and password."""

    email: EmailStr = Field(..., description='Email address')
    password: str = Field(..., min_length=1, max_length=128, description='Password')
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    # Принимается только ради контракта: это вход/линковка существующего аккаунта,
    # а CID возвращающегося пользователя мы не переписываем (см. _capture_yandex_cid).
    yandex_cid: YandexCid = Field(None, description=YANDEX_CID_DESCRIPTION)


class RefreshTokenRequest(BaseModel):
    """Request to refresh access token."""

    refresh_token: str = Field(..., max_length=2048, description='Refresh token')


class PasswordForgotRequest(BaseModel):
    """Request to initiate password reset."""

    email: EmailStr = Field(..., description='Email address')


class PasswordResetRequest(BaseModel):
    """Request to reset password with token."""

    token: str = Field(..., max_length=2048, description='Password reset token')
    password: str = Field(..., min_length=8, max_length=128, description='New password (min 8 chars)')


class AutoLoginRequest(BaseModel):
    """Request for auto-login from guest purchase success page."""

    token: str = Field(..., max_length=2048, description='Auto-login JWT token')


class TokenResponse(BaseModel):
    """Token pair response."""

    access_token: str
    refresh_token: str
    token_type: str = 'bearer'
    expires_in: int = Field(..., description='Access token expiration in seconds')


class UserResponse(BaseModel):
    """User data response."""

    id: int
    telegram_id: int | None = None  # Nullable для email-only пользователей
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    email_verified: bool = False
    balance_kopeks: int = 0
    balance_rubles: float = 0.0
    referral_code: str | None = None
    language: str = 'ru'
    created_at: datetime
    auth_type: str = 'telegram'  # "telegram" или "email"

    class Config:
        from_attributes = True


class EmailRegisterStandaloneRequest(BaseModel):
    """Request to register new account with email (no Telegram required)."""

    email: EmailStr = Field(..., description='Email address')
    password: str = Field(..., min_length=8, max_length=128, description='Password (min 8 chars)')
    first_name: str | None = Field(None, max_length=64, description='First name')
    language: str = Field('ru', max_length=5, pattern=r'^[a-z]{2}$', description='Preferred language (ISO 639-1)')
    referral_code: str | None = Field(
        None, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$', description='Referral code of inviter'
    )
    campaign_slug: str | None = Field(
        None, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$', description='Campaign slug from web link'
    )
    accepted_legal_documents: list[str] | None = Field(
        None,
        max_length=8,
        description=(
            'Ключи документов, с которыми пользователь согласился на экране первой авторизации '
            '(см. GET /cabinet/info/legal-consent).'
        ),
    )
    yandex_cid: YandexCid = Field(None, description=YANDEX_CID_DESCRIPTION)  # см. YandexCid выше


class CampaignBonusInfo(BaseModel):
    """Info about campaign bonus applied during auth."""

    campaign_name: str
    bonus_type: str
    balance_kopeks: int = 0
    subscription_days: int | None = None
    tariff_name: str | None = None


class AuthResponse(BaseModel):
    """Full authentication response with tokens and user."""

    access_token: str
    refresh_token: str
    token_type: str = 'bearer'
    expires_in: int
    user: UserResponse
    campaign_bonus: CampaignBonusInfo | None = None


class RegisterResponse(BaseModel):
    """Response for email registration (before verification)."""

    message: str = Field(..., description='Success message')
    email: str = Field(..., description='Email address to verify')
    requires_verification: bool = Field(True, description='Whether email verification is required')


class EmailChangeRequest(BaseModel):
    """Request to initiate email change."""

    new_email: EmailStr = Field(..., description='New email address')


class EmailChangeVerifyRequest(BaseModel):
    """Request to verify email change with code."""

    code: str = Field(..., min_length=6, max_length=6, pattern=r'^\d{6}$', description='6-digit verification code')


class EmailMergeVerifyRequest(BaseModel):
    """Request to confirm an email account merge with the emailed code."""

    code: str = Field(..., min_length=6, max_length=6, pattern=r'^\d{6}$', description='6-digit confirmation code')


class EmailChangeResponse(BaseModel):
    """Response for email change initiation."""

    message: str = Field(..., description='Success message')
    new_email: str = Field(..., description='New email address pending verification')
    expires_in_minutes: int = Field(..., description='Code expiration time in minutes')


class DeepLinkTokenResponse(BaseModel):
    """Response with deep link auth token."""

    token: str = Field(..., description='One-time auth token')
    bot_username: str = Field(..., description='Bot username for deep link')
    expires_in: int = Field(..., description='Token TTL in seconds')


class DeepLinkPollRequest(BaseModel):
    """Request to poll deep link auth status.

    Deep link auth is always for existing bot users — referral codes are not applicable here.
    Only campaign_slug is supported (campaign bonus can apply to existing users).
    """

    token: str = Field(..., min_length=16, max_length=128, description='Deep link auth token')
    campaign_slug: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        pattern=r'^[a-zA-Z0-9_-]+$',
        description='Campaign slug captured from cabinet URL',
    )
