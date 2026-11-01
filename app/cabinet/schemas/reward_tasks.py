"""Схемы «Заданий» (reward tasks) для кабинета: пользователь и админка.

Правила полей задания живут в сервисе (``validate_task_fields``) — схема лишь
зовёт их, чтобы ошибка пришла привычным 422 с указанием поля.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.reward_task_service import (
    DEFAULT_MAX_PHOTOS,
    DEFAULT_MIN_BOOSTS,
    MAX_BULK_IDS,
    MAX_REJECT_REASON_LENGTH,
    RewardTaskError,
    validate_task_fields,
)


RewardTaskKindLiteral = Literal['photo_report', 'channel_boost']
SubmissionStatusLiteral = Literal['pending', 'approved', 'rejected']


# ==================== Пользователь ====================


class RewardTaskItem(BaseModel):
    """Задание и его состояние для текущего пользователя."""

    id: int
    kind: str
    title: str
    description: str
    reward_days: int
    action_url: str | None = None
    min_boosts: int
    max_photos: int
    status: str  # available | pending | completed | rejected
    reject_reason: str | None = None
    completed_at: datetime | None = None


class RewardTasksResponse(BaseModel):
    eligible: bool
    # no_subscription | restricted; скрытому разделу (tasks_unavailable) роут отвечает 404
    ineligible_reason: str | None = None
    # Буст бот проверяет по привязанному Telegram: без него кабинет сразу показывает
    # «Привяжите Telegram» вместо кнопки проверки. Из базы на каждый запрос — пользователь
    # кабинета после привязки не обновляется, а список задач перечитывается.
    telegram_linked: bool
    tasks: list[RewardTaskItem]


class RewardTasksCountResponse(BaseModel):
    count: int


class RewardTaskMediaInput(BaseModel):
    """Скриншот отчёта: file_id из ``POST /cabinet/media/upload``.

    Формат file_id проверяет сервис (ошибка с кодом ``invalid_media``), здесь —
    только грубые границы размера запроса.
    """

    file_id: str = Field(..., max_length=512)
    file_unique_id: str | None = Field(None, max_length=128)
    thumb_file_id: str | None = Field(None, max_length=512)


class RewardTaskSubmitRequest(BaseModel):
    # Точный лимит (max_photos задания, 500 символов) проверяет сервис с понятным
    # кодом ошибки; здесь — только защита от гигантского тела запроса.
    media: list[RewardTaskMediaInput] = Field(default_factory=list, max_length=50)
    comment: str | None = Field(None, max_length=5000)


class RewardTaskSubmissionResponse(BaseModel):
    id: int
    status: str
    created_at: datetime


class RewardTaskBoostResponse(BaseModel):
    completed: bool
    reward_days: int


# ==================== Админка: задания ====================


class AdminRewardTaskResponse(BaseModel):
    id: int
    kind: str
    title: str
    description: str
    reward_days: int
    action_url: str | None = None
    channel_id: str | None = None
    min_boosts: int
    max_photos: int
    is_active: bool
    sort_order: int
    pending_count: int = 0
    approved_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AdminRewardTasksResponse(BaseModel):
    items: list[AdminRewardTaskResponse]


class AdminRewardTaskWriteRequest(BaseModel):
    """Полное состояние задания (создание и итог слияния при PATCH)."""

    kind: RewardTaskKindLiteral
    title: str
    description: str | None = ''
    reward_days: int
    action_url: str | None = None
    channel_id: str | None = None
    min_boosts: int = DEFAULT_MIN_BOOSTS
    max_photos: int = DEFAULT_MAX_PHOTOS
    is_active: bool = True
    sort_order: int = 0

    @model_validator(mode='after')
    def _apply_service_rules(self) -> 'AdminRewardTaskWriteRequest':
        try:
            normalized = validate_task_fields(self.model_dump())
        except RewardTaskError as error:
            field = error.params.get('field') or 'task'
            raise ValueError(f'{field}: {error.params.get("reason") or "invalid value"}') from None
        for key, value in normalized.items():
            setattr(self, key, value)
        return self


class AdminRewardTaskPatchRequest(BaseModel):
    """Частичная правка: непереданные поля остаются как были.

    Лишние поля (например, весь объект задания со счётчиками) игнорируются.
    """

    model_config = ConfigDict(extra='ignore')

    kind: RewardTaskKindLiteral | None = None
    title: str | None = None
    description: str | None = None
    reward_days: int | None = None
    action_url: str | None = None
    channel_id: str | None = None
    min_boosts: int | None = None
    max_photos: int | None = None
    is_active: bool | None = None
    sort_order: int | None = None

    @field_validator('kind', 'title', 'reward_days', 'min_boosts', 'max_photos', 'is_active', 'sort_order')
    @classmethod
    def _not_null(cls, value):
        # null для обязательного поля — это не «оставить как было», а ошибка:
        # иначе слияние тихо подставило бы старое значение.
        if value is None:
            raise ValueError('must not be null')
        return value


# ==================== Админка: отчёты ====================


class AdminRewardSubmissionMedia(BaseModel):
    type: str = 'photo'
    file_id: str
    thumb_file_id: str | None = None
    token: str
    thumb_token: str | None = None


class AdminRewardSubmissionTask(BaseModel):
    id: int
    title: str
    kind: str


class AdminRewardSubmissionUser(BaseModel):
    id: int
    telegram_id: int | None = None
    username: str | None = None
    full_name: str | None = None
    # users.status: active | blocked | deleted. Отчёт неактивного пользователя не одобрить (user_blocked).
    status: str


class AdminRewardSubmissionItem(BaseModel):
    id: int
    task: AdminRewardSubmissionTask
    user: AdminRewardSubmissionUser
    status: str
    source: str
    media_items: list[AdminRewardSubmissionMedia]
    comment: str | None = None
    boost_count: int | None = None
    reward_days: int | None = None
    reject_reason: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None
    reviewer_name: str | None = None


class AdminRewardSubmissionsResponse(BaseModel):
    items: list[AdminRewardSubmissionItem]
    total: int
    pending_count: int


class AdminRewardApproveRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=MAX_BULK_IDS)


class AdminRewardRejectRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=MAX_BULK_IDS)
    reason: str = Field(..., min_length=1, max_length=MAX_REJECT_REASON_LENGTH)

    @field_validator('reason')
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError('reason must not be empty')
        return text


class AdminRewardBulkResultItem(BaseModel):
    id: int
    ok: bool
    error_code: str | None = None


class AdminRewardBulkResponse(BaseModel):
    results: list[AdminRewardBulkResultItem]
