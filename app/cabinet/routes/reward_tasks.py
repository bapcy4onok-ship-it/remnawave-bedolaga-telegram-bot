"""«Задания» за бесплатные дни подписки — пользовательская часть кабинета.

Все правила — в ``app/services/reward_task_service.py`` (тот же сервис зовёт
бот); здесь только HTTP. Ошибки — ``detail: {code, message, ...}``: стабильный
код, готовый текст на языке пользователя и параметры (``found``/``required`` у
``not_enough_boosts``, ``max`` у ``too_many_media``).

Раздел виден не всем (``reward_task_service.get_reward_tasks_access``): режим
«только админы», отключённая подписка (ответ владельца 2026-10-09). Скрытому
пользователю каждый роут отвечает так, будто раздела нет: 404 с кодом
``tasks_unavailable`` — проверка идёт первой, до поиска задания, а счётчик
для пункта меню и баннера отдаёт 0.
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.database.models import RewardTaskKind, RewardTaskSubmissionSource, User
from app.services import reward_task_service
from app.services.reward_task_service import RewardTaskError, RewardTaskErrorCode

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.reward_tasks import (
    RewardTaskBoostResponse,
    RewardTaskItem,
    RewardTasksCountResponse,
    RewardTasksResponse,
    RewardTaskSubmissionResponse,
    RewardTaskSubmitRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/tasks', tags=['Cabinet Reward Tasks'])

# Всё, чего нет в таблице, — 400.
_ERROR_HTTP_STATUS: dict[str, int] = {
    RewardTaskErrorCode.TASK_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    RewardTaskErrorCode.TASK_INACTIVE: status.HTTP_404_NOT_FOUND,
    RewardTaskErrorCode.TASKS_UNAVAILABLE: status.HTTP_404_NOT_FOUND,
    RewardTaskErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    RewardTaskErrorCode.ALREADY_COMPLETED: status.HTTP_409_CONFLICT,
    RewardTaskErrorCode.ALREADY_PENDING: status.HTTP_409_CONFLICT,
    RewardTaskErrorCode.NOT_PENDING: status.HTTP_409_CONFLICT,
    RewardTaskErrorCode.TASK_HAS_SUBMISSIONS: status.HTTP_409_CONFLICT,
    RewardTaskErrorCode.RESTRICTED: status.HTTP_403_FORBIDDEN,
    RewardTaskErrorCode.SUBSCRIPTION_DISABLED: status.HTTP_403_FORBIDDEN,
    RewardTaskErrorCode.USER_BLOCKED: status.HTTP_403_FORBIDDEN,
    RewardTaskErrorCode.RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    RewardTaskErrorCode.BOOST_CHECK_FAILED: status.HTTP_502_BAD_GATEWAY,
    RewardTaskErrorCode.PANEL_SYNC_FAILED: status.HTTP_503_SERVICE_UNAVAILABLE,
    RewardTaskErrorCode.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def reward_task_http_error(error: RewardTaskError, language: str | None = None) -> HTTPException:
    """RewardTaskError → HTTPException с ``detail: {code, message, **params}``."""
    detail = {
        **error.params,
        'code': error.code,
        'message': reward_task_service.format_error_message(error, language),
    }
    return HTTPException(
        status_code=_ERROR_HTTP_STATUS.get(error.code, status.HTTP_400_BAD_REQUEST),
        detail=detail,
    )


# Пользовательские роуты отвечают скрытому разделу одинаково. subscription_disabled
# сюда доходит только гонкой (подписку отключили посреди начисления за буст):
# такой пользователь уже скрыт, и для него раздела тоже нет.
_HIDDEN_CODES = frozenset(
    {RewardTaskErrorCode.TASKS_UNAVAILABLE.value, RewardTaskErrorCode.SUBSCRIPTION_DISABLED.value}
)


def _user_http_error(error: RewardTaskError, language: str | None) -> HTTPException:
    """Ошибка пользовательского роута: скрытый раздел — всегда 404 ``tasks_unavailable``."""
    if error.code in _HIDDEN_CODES:
        error = RewardTaskError(RewardTaskErrorCode.TASKS_UNAVAILABLE)
    return reward_task_http_error(error, language)


async def _require_visible(db: AsyncSession, user: User) -> reward_task_service.RewardTasksAccess:
    """Раздел виден пользователю, иначе 404 ``tasks_unavailable`` — до любых других проверок."""
    try:
        return await reward_task_service.require_reward_tasks_access(db, user)
    except RewardTaskError as error:
        raise _user_http_error(error, user.language) from None


def _to_item(view: reward_task_service.RewardTaskView) -> RewardTaskItem:
    task = view.task
    return RewardTaskItem(
        id=task.id,
        kind=task.kind,
        title=task.title,
        description=task.description or '',
        reward_days=task.reward_days,
        action_url=task.action_url,
        min_boosts=task.min_boosts,
        max_photos=task.max_photos,
        status=view.status,
        reject_reason=view.reject_reason,
        completed_at=view.completed_at,
    )


@router.get('', response_model=RewardTasksResponse)
async def list_reward_tasks(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> RewardTasksResponse:
    """Активные задания со статусом для пользователя и признак, можно ли их выполнять."""
    access = await _require_visible(db, user)
    overview = await reward_task_service.list_tasks_for_user(db, user, access=access)
    return RewardTasksResponse(
        eligible=overview.eligible,
        ineligible_reason=overview.ineligible_reason,
        telegram_linked=bool(user.telegram_id),
        tasks=[_to_item(view) for view in overview.tasks],
    )


@router.get('/count', response_model=RewardTasksCountResponse)
async def count_reward_tasks(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> RewardTasksCountResponse:
    """Число активных заданий — флаг для пункта меню и баннера; скрытому разделу — 0."""
    count = await reward_task_service.count_active_tasks(db)
    if count and not (await reward_task_service.get_reward_tasks_access(db, user)).visible:
        count = 0
    return RewardTasksCountResponse(count=count)


@router.post(
    '/{task_id}/submissions',
    response_model=RewardTaskSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_reward_task_report(
    task_id: int,
    payload: RewardTaskSubmitRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> RewardTaskSubmissionResponse:
    """Отправить фотоотчёт по заданию на проверку."""
    language = user.language
    await _require_visible(db, user)
    try:
        task = await reward_task_service.require_active_task(db, task_id, RewardTaskKind.PHOTO_REPORT)
        submission = await reward_task_service.submit_photo_report(
            db,
            user,
            task,
            payload.media,
            payload.comment,
            source=RewardTaskSubmissionSource.CABINET,
        )
    except RewardTaskError as error:
        raise _user_http_error(error, language) from None

    return RewardTaskSubmissionResponse(
        id=submission.id,
        status=submission.status,
        created_at=submission.created_at,
    )


@router.post('/{task_id}/check-boost', response_model=RewardTaskBoostResponse)
async def check_reward_task_boost(
    task_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> RewardTaskBoostResponse:
    """Проверить буст канала и сразу получить награду."""
    language = user.language
    await _require_visible(db, user)
    try:
        task = await reward_task_service.require_active_task(db, task_id, RewardTaskKind.CHANNEL_BOOST)
        async with create_bot() as bot:
            result = await reward_task_service.check_boost(db, user, task, bot)
    except RewardTaskError as error:
        raise _user_http_error(error, language) from None

    return RewardTaskBoostResponse(completed=True, reward_days=result.reward_days)
