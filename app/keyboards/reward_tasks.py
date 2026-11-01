"""Клавиатуры раздела «Задания»: бесплатные дни подписки за задания.

Правила и статусы — в ``app/services/reward_task_service.py``, экраны — в
``app/handlers/reward_tasks.py``. Здесь только кнопки и их callback_data.
callback_data меню и экрана задания и «+N дн.» (``format_reward_days``) живут
в сервисе: они же нужны уведомлениям, а сервис не может импортировать
клавиатуры (цикл). Отсюда их по-прежнему можно импортировать.
"""

from collections.abc import Sequence
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.database.models import RewardTaskKind
from app.localization.loader import DEFAULT_LANGUAGE
from app.localization.texts import get_texts
from app.services.reward_task_service import (
    REWARD_TASKS_MENU_CALLBACK,
    TASK_CALLBACK_PREFIX,
    RewardTaskErrorCode,
    TaskUserStatus,
    format_reward_days,
    normalize_action_url,
)
from app.utils.miniapp_buttons import strip_tg_emoji_tags


CHANNEL_CALLBACK_PREFIX = 'reward_task_channel:'
BOOST_CALLBACK_PREFIX = 'reward_task_boost:'
PHOTO_CALLBACK_PREFIX = 'reward_task_photo:'
PHOTO_DONE_CALLBACK_PREFIX = 'reward_task_done:'
PHOTO_CANCEL_CALLBACK_PREFIX = 'reward_task_cancel:'

STATUS_ICONS = {
    TaskUserStatus.AVAILABLE.value: '🎁',
    TaskUserStatus.PENDING.value: '⏳',
    TaskUserStatus.COMPLETED.value: '✅',
    TaskUserStatus.REJECTED.value: '❌',
}

# Название задания — до 200 символов; на кнопке хватит начала.
TASK_TITLE_BUTTON_LIMIT = 40


def status_icon(status: str) -> str:
    return STATUS_ICONS.get(status, STATUS_ICONS[TaskUserStatus.AVAILABLE.value])


def shorten(text: str, limit: int) -> str:
    value = ' '.join((text or '').split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + '…'


def _button(text: str, **kwargs: Any) -> InlineKeyboardButton:
    # Текст кнопки Telegram не парсит как HTML: <tg-emoji> из override-локалей
    # показался бы сырым тегом.
    return InlineKeyboardButton(text=strip_tg_emoji_tags(text), **kwargs)


def _safe_action_url(url: str | None) -> str | None:
    """Ссылка задания для URL-кнопки. Кривую ссылку Telegram отвергает вместе со
    всем сообщением, поэтому лучше остаться без кнопки, чем без экрана."""
    try:
        return normalize_action_url(url)
    except ValueError:
        return None


def get_reward_tasks_list_keyboard(language: str, views: Sequence[Any]) -> InlineKeyboardMarkup:
    """Список заданий: статус, название и награда на кнопке."""
    texts = get_texts(language)
    rows: list[list[InlineKeyboardButton]] = []
    for view in views:
        task = view.task
        label = (
            f'{status_icon(view.status)} {shorten(task.title, TASK_TITLE_BUTTON_LIMIT)} · '
            f'{format_reward_days(texts, task.reward_days)}'
        )
        rows.append([_button(label, callback_data=f'{TASK_CALLBACK_PREFIX}{task.id}')])
    rows.append([_button(texts.BACK, callback_data='back_to_menu')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_reward_task_detail_keyboard(language: str, view: Any) -> InlineKeyboardMarkup:
    """Экран задания: ссылка, действие (буст / отчёт) и возврат к списку."""
    texts = get_texts(language)
    task = view.task
    is_boost = task.kind == RewardTaskKind.CHANNEL_BOOST
    rows: list[list[InlineKeyboardButton]] = []

    url = _safe_action_url(task.action_url)
    if url and view.status != TaskUserStatus.COMPLETED:
        label = (
            texts.t('REWARD_TASKS_BOOST_LINK_BUTTON', '🚀 Забустить канал')
            if is_boost
            else texts.t('REWARD_TASKS_OPEN_LINK_BUTTON', '🔗 Открыть ссылку')
        )
        rows.append([_button(label, url=url)])

    if view.status in (TaskUserStatus.AVAILABLE, TaskUserStatus.REJECTED):
        if task.kind == RewardTaskKind.CHANNEL_SUBSCRIPTION:
            rows.append(
                [
                    _button(
                        texts.t('REWARD_TASKS_CHECK_CHANNEL_BUTTON', '🔄 Проверить подписку'),
                        callback_data=f'{CHANNEL_CALLBACK_PREFIX}{task.id}',
                    )
                ]
            )
        elif is_boost:
            rows.append(
                [
                    _button(
                        texts.t('REWARD_TASKS_CHECK_BOOST_BUTTON', '🔄 Проверить буст'),
                        callback_data=f'{BOOST_CALLBACK_PREFIX}{task.id}',
                    )
                ]
            )
        elif task.kind == RewardTaskKind.PHOTO_REPORT:
            label = (
                texts.t('REWARD_TASKS_RESEND_REPORT_BUTTON', '📸 Отправить новый отчёт')
                if view.status == TaskUserStatus.REJECTED
                else texts.t('REWARD_TASKS_SEND_REPORT_BUTTON', '📸 Отправить отчёт')
            )
            rows.append([_button(label, callback_data=f'{PHOTO_CALLBACK_PREFIX}{task.id}')])

    rows.append(
        [
            _button(
                texts.t('REWARD_TASKS_BACK_TO_LIST_BUTTON', '⬅️ К заданиям'),
                callback_data=REWARD_TASKS_MENU_CALLBACK,
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_reward_tasks_ineligible_keyboard(
    language: str,
    reason: str | None,
    *,
    show_trial: bool,
) -> InlineKeyboardMarkup:
    """Нет подписки — ведём в триал/покупку. Ограничение от админа и неизвестная
    причина — в поддержку: триал или покупка тут не помогут. (С отключённой
    подпиской раздел пользователю не показывается вовсе.)"""
    texts = get_texts(language)
    rows: list[list[InlineKeyboardButton]] = []
    if reason is None or reason == RewardTaskErrorCode.NO_SUBSCRIPTION:
        if show_trial:
            rows.append([_button(texts.MENU_TRIAL, callback_data='menu_trial')])
        rows.append([_button(texts.MENU_BUY_SUBSCRIPTION, callback_data='menu_buy')])
    else:
        support_url = settings.get_support_contact_url()
        if support_url:
            rows.append([_button(texts.t('REWARD_TASKS_SUPPORT_BUTTON', '🆘 Написать в поддержку'), url=support_url)])
    rows.append([_button(texts.BACK, callback_data='back_to_menu')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_reward_task_photo_keyboard(language: str, task_id: int, photo_count: int) -> InlineKeyboardMarkup:
    """Сбор скриншотов: «Готово» появляется, когда есть что отправить."""
    texts = get_texts(language)
    row: list[InlineKeyboardButton] = []
    if photo_count > 0:
        row.append(
            _button(
                texts.t('REWARD_TASKS_DONE_BUTTON', '✅ Готово'),
                callback_data=f'{PHOTO_DONE_CALLBACK_PREFIX}{task_id}',
            )
        )
    row.append(_button(texts.t('CANCEL', '❌ Отмена'), callback_data=f'{PHOTO_CANCEL_CALLBACK_PREFIX}{task_id}'))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def get_reward_task_result_keyboard(language: str = DEFAULT_LANGUAGE) -> InlineKeyboardMarkup:
    """После отправки отчёта или засчитанного буста."""
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    texts.t('REWARD_TASKS_BACK_TO_LIST_BUTTON', '⬅️ К заданиям'),
                    callback_data=REWARD_TASKS_MENU_CALLBACK,
                )
            ],
            [_button(texts.BACK_TO_MENU, callback_data='back_to_menu')],
        ]
    )
