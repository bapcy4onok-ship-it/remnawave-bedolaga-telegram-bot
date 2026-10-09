"""Админка «Заданий и фотоотчётов»: задания и проверка отчётов.

Правила (что можно править и удалять, как начисляется награда) — в
``app/services/reward_task_service.py``. Статические пути объявлены раньше
``/{task_id}``; список — на ``''``, потому что у кабинетного роутера
``redirect_slashes=False``.
"""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.routes.media import make_stable_media_token
from app.database.models import RewardTask, RewardTaskSubmission, User, UserStatus
from app.services import reward_task_service
from app.services.reward_task_service import RewardTaskError, RewardTaskErrorCode

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.reward_tasks import (
    AdminRewardApproveRequest,
    AdminRewardBulkResponse,
    AdminRewardBulkResultItem,
    AdminRewardRejectRequest,
    AdminRewardSubmissionItem,
    AdminRewardSubmissionMedia,
    AdminRewardSubmissionsResponse,
    AdminRewardSubmissionTask,
    AdminRewardSubmissionUser,
    AdminRewardTaskPatchRequest,
    AdminRewardTaskResponse,
    AdminRewardTasksResponse,
    AdminRewardTaskWriteRequest,
    SubmissionStatusLiteral,
)
from .reward_tasks import reward_task_http_error


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/tasks', tags=['Cabinet Admin Reward Tasks'])

# Тексты для кодов, которые бывают только в админке (у пользовательских — локализованный текст сервиса).
_ADMIN_ERROR_MESSAGES = {
    RewardTaskErrorCode.TASK_HAS_SUBMISSIONS: 'The task already has submissions: deactivate it instead',
    RewardTaskErrorCode.INVALID_REASON: 'Reject reason must not be empty',
    RewardTaskErrorCode.INVALID_TASK: 'Invalid task fields',
    RewardTaskErrorCode.NOT_FOUND: 'Submission not found',
    RewardTaskErrorCode.NOT_PENDING: 'Submission is already processed',
}


def _admin_http_error(error: RewardTaskError) -> HTTPException:
    exc = reward_task_http_error(error)
    message = _ADMIN_ERROR_MESSAGES.get(error.code)
    if message is not None:
        field = error.params.get('field')
        exc.detail['message'] = f'{field}: {error.params.get("reason")}' if field else message
    return exc


def _task_response(task: RewardTask, pending_count: int = 0, approved_count: int = 0) -> AdminRewardTaskResponse:
    return AdminRewardTaskResponse(
        id=task.id,
        kind=task.kind,
        title=task.title,
        description=task.description or '',
        reward_days=task.reward_days,
        action_url=task.action_url,
        channel_id=task.channel_id,
        min_boosts=task.min_boosts,
        max_photos=task.max_photos,
        is_active=task.is_active,
        sort_order=task.sort_order,
        pending_count=pending_count,
        approved_count=approved_count,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def _task_response_with_counts(db: AsyncSession, task: RewardTask) -> AdminRewardTaskResponse:
    entries = await reward_task_service.list_tasks_with_counts(db, task_ids=[task.id])
    if entries:
        return _task_response(entries[0].task, entries[0].pending_count, entries[0].approved_count)
    return _task_response(task)


def _media_response(raw_items: Any) -> list[AdminRewardSubmissionMedia]:
    """Скриншоты с подписанными ссылками; без превью — превью делаем из полного файла."""
    media: list[AdminRewardSubmissionMedia] = []
    for item in raw_items or []:
        if not isinstance(item, dict) or not isinstance(item.get('file_id'), str):
            continue
        file_id = item['file_id']
        thumb_id = item.get('thumb_file_id') if isinstance(item.get('thumb_file_id'), str) else None
        thumb_id = thumb_id or file_id
        media.append(
            AdminRewardSubmissionMedia(
                type=item.get('type') or 'photo',
                file_id=file_id,
                thumb_file_id=thumb_id,
                # Стабильные в пределах 6 ч: перезапрос очереди не меняет ссылки на
                # картинки, и браузер не качает превью через Telegram заново.
                token=make_stable_media_token(file_id),
                thumb_token=make_stable_media_token(thumb_id),
            )
        )
    return media


def _submission_response(submission: RewardTaskSubmission) -> AdminRewardSubmissionItem:
    task = submission.task
    user = submission.user
    reviewer = submission.reviewer
    return AdminRewardSubmissionItem(
        id=submission.id,
        task=AdminRewardSubmissionTask(
            id=submission.task_id,
            title=task.title if task else '',
            kind=task.kind if task else '',
        ),
        user=AdminRewardSubmissionUser(
            id=submission.user_id,
            telegram_id=user.telegram_id if user else None,
            username=user.username if user else None,
            full_name=user.full_name if user else None,
            # NULL — старые строки до дефолта колонки: бот и начисление считают их активными.
            status=(user.status if user else None) or UserStatus.ACTIVE.value,
        ),
        status=submission.status,
        source=submission.source,
        media_items=_media_response(submission.media_items),
        comment=submission.comment,
        boost_count=submission.boost_count,
        reward_days=submission.reward_days,
        reject_reason=submission.reject_reason,
        created_at=submission.created_at,
        reviewed_at=submission.reviewed_at,
        reviewer_name=(reviewer.full_name or None) if reviewer else None,
    )


async def _get_task_or_404(db: AsyncSession, task_id: int) -> RewardTask:
    task = await reward_task_service.get_task(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={'code': 'task_not_found', 'message': 'Task not found'},
        )
    return task


# ==================== Отчёты (статические пути — до /{task_id}) ====================


@router.get('/submissions', response_model=AdminRewardSubmissionsResponse)
async def list_reward_task_submissions(
    submission_status: SubmissionStatusLiteral | None = Query(None, alias='status'),
    task_id: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    admin: User = Depends(require_permission('tasks:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminRewardSubmissionsResponse:
    """Отчёты: на проверке — старые первыми, история — последние обработанные первыми."""
    items, total, pending_count = await reward_task_service.list_submissions(
        db, status=submission_status, task_id=task_id, offset=offset, limit=limit
    )
    return AdminRewardSubmissionsResponse(
        items=[_submission_response(item) for item in items],
        total=total,
        pending_count=pending_count,
    )


@router.post('/submissions/approve', response_model=AdminRewardBulkResponse)
async def approve_reward_task_submissions(
    payload: AdminRewardApproveRequest,
    admin: User = Depends(require_permission('tasks:approve')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminRewardBulkResponse:
    """Одобрить выбранные отчёты — каждый отдельно, с результатом по каждому id."""
    results = await reward_task_service.approve(db, payload.ids, admin)
    return AdminRewardBulkResponse(
        results=[AdminRewardBulkResultItem(id=r.id, ok=r.ok, error_code=r.error_code) for r in results]
    )


@router.post('/submissions/reject', response_model=AdminRewardBulkResponse)
async def reject_reward_task_submissions(
    payload: AdminRewardRejectRequest,
    admin: User = Depends(require_permission('tasks:reject')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminRewardBulkResponse:
    """Отклонить выбранные отчёты с причиной — пользователь увидит её и сможет прислать новый."""
    try:
        results = await reward_task_service.reject(db, payload.ids, admin, payload.reason)
    except RewardTaskError as error:
        raise _admin_http_error(error) from None
    return AdminRewardBulkResponse(
        results=[AdminRewardBulkResultItem(id=r.id, ok=r.ok, error_code=r.error_code) for r in results]
    )


# ==================== Задания ====================


@router.get('', response_model=AdminRewardTasksResponse)
async def list_reward_tasks_admin(
    admin: User = Depends(require_permission('tasks:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminRewardTasksResponse:
    """Все задания, включая выключенные, со счётчиками отчётов."""
    entries = await reward_task_service.list_tasks_with_counts(db)
    return AdminRewardTasksResponse(
        items=[_task_response(entry.task, entry.pending_count, entry.approved_count) for entry in entries]
    )


@router.post('', response_model=AdminRewardTaskResponse, status_code=status.HTTP_201_CREATED)
async def create_reward_task(
    payload: AdminRewardTaskWriteRequest,
    admin: User = Depends(require_permission('tasks:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminRewardTaskResponse:
    """Создать задание."""
    try:
        task = await reward_task_service.create_task(db, payload.model_dump())
    except RewardTaskError as error:
        raise _admin_http_error(error) from None
    logger.info('Админ создал задание', task_id=task.id, admin_id=admin.id)
    return _task_response(task)


@router.patch('/{task_id}', response_model=AdminRewardTaskResponse)
async def update_reward_task(
    task_id: int,
    payload: AdminRewardTaskPatchRequest,
    admin: User = Depends(require_permission('tasks:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminRewardTaskResponse:
    """Изменить задание: непереданные поля остаются как были, проверяется итоговое состояние."""
    task = await _get_task_or_404(db, task_id)
    try:
        merged = AdminRewardTaskWriteRequest(
            **{**reward_task_service.task_fields(task), **payload.model_dump(exclude_unset=True)}
        )
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error

    try:
        task = await reward_task_service.update_task(db, task, merged.model_dump())
    except RewardTaskError as error:
        raise _admin_http_error(error) from None
    logger.info('Админ изменил задание', task_id=task.id, admin_id=admin.id)
    return await _task_response_with_counts(db, task)


@router.delete('/{task_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_reward_task(
    task_id: int,
    admin: User = Depends(require_permission('tasks:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> None:
    """Удалить задание без отчётов. С отчётами — 409: такое задание только выключают."""
    task = await _get_task_or_404(db, task_id)
    try:
        await reward_task_service.delete_task(db, task)
    except RewardTaskError as error:
        raise _admin_http_error(error) from None
    logger.info('Админ удалил задание', task_id=task_id, admin_id=admin.id)
