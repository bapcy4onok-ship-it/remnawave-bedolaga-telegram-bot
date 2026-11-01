"""«Задания» в боте: бесплатные дни подписки за выполненные задания.

Правила (кто может выполнять, что засчитывается, как начисляются дни) живут в
``app/services/reward_task_service.py`` — тот же сервис обслуживает кабинет.
Здесь только экраны и сбор скриншотов.

Входы: кнопка главного меню ``reward_tasks_menu``, команда ``/tasks`` и диплинк
``/start tasks`` (его разбирает ``cmd_start`` раньше кампаний и рефкодов).

Раздел виден не всем (``reward_task_service.get_reward_tasks_access``): пока
выключен REWARD_TASKS_FOR_ALL_USERS — только админам, а пользователю с
отключённой подпиской — никогда (ответ владельца 2026-10-09). Скрытому разделу
нет кнопки в меню, ``/start tasks`` открывает обычный старт, а старые кнопки,
``/tasks`` и сбор скриншотов отвечают коротким «Раздел недоступен» и
сбрасывают зависший сбор отчёта.

Фотоотчёт собирается в FSM ``RewardTaskStates.waiting_for_photos``: скриншоты
(по одному или альбомом) копятся в state data, «Готово» создаёт отчёт на
проверку. Альбом приходит пачкой отдельных апдейтов, которые бот обрабатывает
параллельно, поэтому чтение-изменение-запись state идёт под замком на
пользователя. Отвечает бот одним сообщением-статусом на всю пачку: статус
публикуется через ``STATUS_DEBOUNCE_SECONDS`` после последнего апдейта.

Кнопки под уведомлениями о проверке отчёта (их шлёт сервис) ведут сюда же:
``reward_tasks_menu`` и ``reward_task:<id>``.
"""

import asyncio
import html
import itertools
import weakref
from typing import Any

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import RewardTaskKind, RewardTaskSubmissionSource, User
from app.keyboards.inline import get_back_keyboard
from app.keyboards.reward_tasks import (
    BOOST_CALLBACK_PREFIX,
    CHANNEL_CALLBACK_PREFIX,
    PHOTO_CALLBACK_PREFIX,
    PHOTO_CANCEL_CALLBACK_PREFIX,
    PHOTO_DONE_CALLBACK_PREFIX,
    REWARD_TASKS_MENU_CALLBACK,
    TASK_CALLBACK_PREFIX,
    format_reward_days,
    get_reward_task_detail_keyboard,
    get_reward_task_photo_keyboard,
    get_reward_task_result_keyboard,
    get_reward_tasks_ineligible_keyboard,
    get_reward_tasks_list_keyboard,
    shorten,
    status_icon,
)
from app.localization.texts import get_texts
from app.services import reward_task_service as rts
from app.services.reward_task_service import RewardTaskError, RewardTaskErrorCode, TaskUserStatus
from app.states import RewardTaskStates
from app.utils.decorators import error_handler
from app.utils.miniapp_buttons import strip_tg_emoji_tags
from app.utils.photo_message import edit_or_answer_photo
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

# /start tasks — ссылка на раздел из канала или кабинета
TASKS_START_PARAMETER = 'tasks'

# Пауза перед статусом: альбом из 10 фото приходит за доли секунды, и ответ
# нужен один на всю пачку (иначе десяток сообщений и флуд-лимит Telegram).
STATUS_DEBOUNCE_SECONDS = 1.0

INELIGIBLE_PREVIEW_LIMIT = 10
PREVIEW_TITLE_LIMIT = 60
CALLBACK_ALERT_LIMIT = 200  # лимит текста answerCallbackQuery
# Начало описания в сообщении-статусе: напоминание, что снимать, а статус
# остаётся коротким. Описание целиком — в подсказке и на экране задания.
STATUS_EXCERPT_LIMIT = 150

# Ключи state data сценария сбора скриншотов
_TASK_ID_KEY = 'reward_task_id'
_TASK_TITLE_KEY = 'reward_task_title'
_TASK_EXCERPT_KEY = 'reward_task_excerpt'
_MAX_PHOTOS_KEY = 'reward_task_max_photos'
_PHOTOS_KEY = 'reward_task_photos'
_COMMENT_KEY = 'reward_task_comment'
_STATUS_CHAT_KEY = 'reward_task_status_chat_id'
_STATUS_MESSAGE_KEY = 'reward_task_status_message_id'
_OVERFLOW_KEY = 'reward_task_overflow'
_NOT_PHOTO_KEY = 'reward_task_not_photo'

# После этих ошибок «Готово» отчёт можно собрать заново, не выходя из сценария
# (например, админ уменьшил лимит скриншотов, пока пользователь их присылал).
_MEDIA_ERRORS = frozenset({RewardTaskErrorCode.TOO_MANY_MEDIA.value, RewardTaskErrorCode.INVALID_MEDIA.value})
# Причины, по которым задания недоступны видимому разделу (get_ineligible_reason):
# вместо алерта показываем экран «почему нельзя». Остальные коды — алерт;
# неизвестный код (и user_blocked, который сюда не доходит) — общий текст ошибки.
_INELIGIBLE_ERRORS = frozenset(
    {
        RewardTaskErrorCode.NO_SUBSCRIPTION.value,
        RewardTaskErrorCode.RESTRICTED.value,
    }
)
# Раздел скрылся посреди действия (подписку отключили, переключатель выключили):
# ответ как у скрытого раздела — нейтральный алерт, без экранов.
_HIDDEN_ERRORS = frozenset(
    {
        RewardTaskErrorCode.TASKS_UNAVAILABLE.value,
        RewardTaskErrorCode.SUBSCRIPTION_DISABLED.value,
    }
)
_LIST_ERRORS = frozenset({RewardTaskErrorCode.TASK_NOT_FOUND.value, RewardTaskErrorCode.TASK_INACTIVE.value})
_DETAIL_ERRORS = frozenset(
    {
        RewardTaskErrorCode.ALREADY_COMPLETED.value,
        RewardTaskErrorCode.ALREADY_PENDING.value,
        RewardTaskErrorCode.WRONG_TASK_KIND.value,
    }
)


# ── Замок и отложенный статус ────────────────────────────────────────────────

# Замок на пользователя: апдейты альбома обрабатываются параллельно, а
# FSMContext.update_data — это чтение и запись без атомарности. Слабые ссылки:
# замок живёт, пока его кто-то держит или ждёт.
_user_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()

# Каждый новый апдейт перебивает токен отложенного статуса — публикует только
# последний. Токены не повторяются, поэтому «проснувшаяся» старая задача не
# примет себя за актуальную.
_status_tokens: dict[int, int] = {}
_status_token_counter = itertools.count(1)
_status_tasks: set[asyncio.Task] = set()


def _user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def _schedule_status(bot: Bot, *, user_id: int, chat_id: int, state: FSMContext, language: str) -> None:
    token = next(_status_token_counter)
    _status_tokens[user_id] = token
    task = asyncio.create_task(
        _publish_status_later(bot, user_id=user_id, chat_id=chat_id, state=state, language=language, token=token)
    )
    _status_tasks.add(task)
    task.add_done_callback(_status_tasks.discard)


def _cancel_status(user_id: int) -> None:
    _status_tokens.pop(user_id, None)


async def _publish_status_later(
    bot: Bot,
    *,
    user_id: int,
    chat_id: int,
    state: FSMContext,
    language: str,
    token: int,
) -> None:
    try:
        await asyncio.sleep(STATUS_DEBOUNCE_SECONDS)
        if _status_tokens.get(user_id) != token:
            return
        async with _user_lock(user_id):
            if _status_tokens.get(user_id) != token:
                return
            _status_tokens.pop(user_id, None)
            await _publish_status(bot, chat_id=chat_id, state=state, language=language)
    except Exception as error:
        logger.warning('Не удалось показать статус фотоотчёта', user_id=user_id, error=str(error))


async def _publish_status(bot: Bot, *, chat_id: int, state: FSMContext, language: str) -> None:
    """Новое сообщение-статус внизу чата; прежнее (с кнопками) удаляется."""
    if await state.get_state() != RewardTaskStates.waiting_for_photos.state:
        return
    data = await state.get_data()
    texts = get_texts(language)
    count = len(data.get(_PHOTOS_KEY) or [])
    text = _photo_status_text(
        texts,
        count,
        _max_photos(data),
        overflow=bool(data.get(_OVERFLOW_KEY)),
        not_photo=bool(data.get(_NOT_PHOTO_KEY)),
    )
    # Подсказка с описанием задания удаляется вместе с прежним статусом —
    # напоминание о задании переезжает в новый статус.
    reminder = _task_reminder_text(texts, data)
    if reminder:
        text = f'{reminder}\n\n{text}'
    keyboard = get_reward_task_photo_keyboard(language, data.get(_TASK_ID_KEY), count)
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode='HTML')
    await state.update_data(
        {
            _STATUS_CHAT_KEY: chat_id,
            _STATUS_MESSAGE_KEY: sent.message_id,
            _OVERFLOW_KEY: False,
            _NOT_PHOTO_KEY: False,
        }
    )
    await _delete_status_message(bot, data, keep_message_id=sent.message_id)


async def _delete_status_message(bot: Bot, data: dict[str, Any], *, keep_message_id: int | None = None) -> None:
    """Убирает прежний статус: его «Готово»/«Отмена» больше не актуальны."""
    chat_id = data.get(_STATUS_CHAT_KEY)
    message_id = data.get(_STATUS_MESSAGE_KEY)
    if not chat_id or not message_id or message_id == keep_message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as error:
        logger.debug('Не удалось удалить статус фотоотчёта', message_id=message_id, error=str(error))


async def _leave_photo_flow(
    state: FSMContext,
    bot: Bot,
    user_id: int,
    *,
    keep_message_id: int | None = None,
) -> None:
    """Вход в раздел сбрасывает state; недособранный отчёт отменяется вместе со статусом."""
    async with _user_lock(user_id):
        in_flow = await state.get_state() == RewardTaskStates.waiting_for_photos.state
        data = await state.get_data() if in_flow else {}
        _cancel_status(user_id)
        await state.clear()
    if in_flow:
        await _delete_status_message(bot, data, keep_message_id=keep_message_id)


async def _is_collecting(state: FSMContext, data: dict[str, Any], task_id: int | None) -> bool:
    """Кнопка «Готово»/«Отмена» относится к текущему сбору, а не к старому сообщению."""
    if task_id is None or data.get(_TASK_ID_KEY) != task_id:
        return False
    return await state.get_state() == RewardTaskStates.waiting_for_photos.state


# ── Мелочи ───────────────────────────────────────────────────────────────────


def _parse_task_id(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    raw = data[len(prefix) :]
    if not raw.isascii() or not raw.isdigit():
        return None
    return int(raw)


def _message_id(callback: types.CallbackQuery) -> int | None:
    return getattr(callback.message, 'message_id', None)


def _max_photos(data: dict[str, Any]) -> int:
    try:
        value = int(data.get(_MAX_PHOTOS_KEY) or rts.DEFAULT_MAX_PHOTOS)
    except (TypeError, ValueError):
        value = rts.DEFAULT_MAX_PHOTOS
    return max(1, min(value, rts.MAX_PHOTOS_LIMIT))


def _photo_key(item: dict[str, Any]) -> str | None:
    return item.get('file_unique_id') or item.get('file_id')


def _contains_photo(photos: list[dict[str, Any]], item: dict[str, Any]) -> bool:
    key = _photo_key(item)
    return any(_photo_key(photo) == key for photo in photos)


def _merge_comment(current: str | None, caption: str | None) -> str | None:
    """Подписи к фото → комментарий для проверяющего (без повторов, в пределах лимита)."""
    text = (caption or '').strip()
    if not text or (current and text in current):
        return current
    merged = f'{current}\n{text}' if current else text
    return merged[: rts.MAX_COMMENT_LENGTH]


def _trial_available(db_user: User) -> bool:
    """Показывать ли кнопку триала; экран триала всё равно перепроверит сам."""
    if settings.TRIAL_DURATION_DAYS <= 0 or settings.is_trial_disabled_for_user(
        getattr(db_user, 'auth_type', 'telegram')
    ):
        return False
    try:
        return not db_user.is_trial_already_used()
    except Exception:
        # subscriptions не загружены — решит экран триала
        return not getattr(db_user, 'has_had_paid_subscription', False)


def _is_expired_query(error: TelegramBadRequest) -> bool:
    message = str(error).lower()
    return 'query is too old' in message or 'query id is invalid' in message


async def _answer_alert(callback: types.CallbackQuery, text: str, *, message_if_expired: bool = False) -> None:
    """Алерт на нажатие кнопки.

    ``message_if_expired`` — для ответа после долгой операции: начисление за буст
    ждёт панель (проверка до ``PANEL_LOOKUP_TIMEOUT_SECONDS`` и синхронизация до
    ``PANEL_SYNC_TIMEOUT_SECONDS``, ~13 с), а на такой старый callback Telegram
    алерт уже не принимает. Тогда тот же текст уходит сообщением в чат — иначе
    пользователь не узнал бы, что дни не начислены.
    """
    # Текст алерта Telegram показывает как есть, без HTML
    value = strip_tg_emoji_tags(text or '')
    alert = value if len(value) <= CALLBACK_ALERT_LIMIT else value[: CALLBACK_ALERT_LIMIT - 1] + '…'
    try:
        await callback.answer(alert, show_alert=True)
    except TelegramBadRequest as error:
        if not message_if_expired or not _is_expired_query(error):
            raise
        chat = getattr(callback.message, 'chat', None)
        chat_id = chat.id if chat is not None else callback.from_user.id
        logger.warning('Ответ на кнопку опоздал: Telegram не принял алерт — отправляем сообщением', chat_id=chat_id)
        try:
            await callback.bot.send_message(chat_id=chat_id, text=value, parse_mode=None)
        except Exception as send_error:
            logger.warning('Не удалось отправить ответ на кнопку сообщением', chat_id=chat_id, error=str(send_error))


def _is_review_notice(message: Any) -> bool:
    """Уведомление о проверке отчёта: текст с одной кнопкой раздела.

    Его шлёт сервис (``_build_notice``): «🎁 Задания» (``reward_tasks_menu``)
    или «📸 Отправить новый отчёт» (``reward_task:<id>``). Экраны раздела в
    режиме логотипа — фото, так что текст с такой кнопкой — это уведомление
    (или экран, не влезший в подпись к фото: ему новое сообщение тоже не вредит).
    """
    if message is None or isinstance(message, InaccessibleMessage) or getattr(message, 'photo', None):
        return False
    markup = getattr(message, 'reply_markup', None)
    buttons = [button for row in getattr(markup, 'inline_keyboard', None) or [] for button in row]
    if len(buttons) != 1:
        return False
    data = buttons[0].callback_data or ''
    return data == REWARD_TASKS_MENU_CALLBACK or data.startswith(TASK_CALLBACK_PREFIX)


async def _show_screen(callback: types.CallbackQuery, text: str, keyboard: types.InlineKeyboardMarkup) -> None:
    if settings.ENABLE_LOGO_MODE and _is_review_notice(callback.message):
        # Текст в фото с логотипом не превратить: общий фоллбек удалил бы
        # уведомление (а с ним причину отказа) и прислал экран заново. Экран
        # приходит новым сообщением, уведомление остаётся в чате. Без логотипа
        # уведомление просто редактируется в экран — причина отказа есть и там.
        await callback.message.answer(text, reply_markup=keyboard, parse_mode='HTML')
        return
    await edit_or_answer_photo(callback=callback, caption=text, keyboard=keyboard, parse_mode='HTML')


# ── Тексты экранов ───────────────────────────────────────────────────────────


def _title(texts: Any) -> str:
    return texts.t('REWARD_TASKS_TITLE', '🎁 <b>Задания</b>')


def _list_text(texts: Any) -> str:
    return '\n\n'.join(
        [
            _title(texts),
            texts.t('REWARD_TASKS_INTRO', 'Выполняйте задания и получайте бесплатные дни подписки.'),
            texts.t('REWARD_TASKS_LEGEND', '🎁 доступно · ⏳ на проверке · ✅ выполнено · ❌ отклонено'),
        ]
    )


def _empty_text(texts: Any) -> str:
    return '\n\n'.join([_title(texts), texts.t('REWARD_TASKS_EMPTY', 'Сейчас нет доступных заданий. Загляните позже!')])


def _ineligible_text(texts: Any, language: str, reason: str | None, views: list[Any]) -> str:
    """Почему задания недоступны; без подписки — ещё и что можно будет заработать.

    Превью — только тем, кто откроет задания триалом или покупкой. Ограниченному
    админом и с отключённой подпиской список ничего не даёт.
    """
    reason = reason or RewardTaskErrorCode.NO_SUBSCRIPTION.value
    parts = [_title(texts), html.escape(rts.format_error_message(reason, language))]
    if reason == RewardTaskErrorCode.NO_SUBSCRIPTION and views:
        lines = [
            f'• {html.escape(shorten(view.task.title, PREVIEW_TITLE_LIMIT))} — '
            f'{format_reward_days(texts, view.task.reward_days)}'
            for view in views[:INELIGIBLE_PREVIEW_LIMIT]
        ]
        header = texts.t('REWARD_TASKS_PREVIEW_HEADER', 'Задания, которые станут доступны:')
        parts.append(header + '\n' + '\n'.join(lines))
    return '\n\n'.join(parts)


def _status_line(texts: Any, view: Any) -> str:
    task = view.task
    if view.status == TaskUserStatus.COMPLETED:
        return texts.t('REWARD_TASKS_STATUS_COMPLETED', '✅ Задание выполнено — награда начислена.')
    if view.status == TaskUserStatus.PENDING:
        return texts.t(
            'REWARD_TASKS_STATUS_PENDING',
            '⏳ Отчёт на проверке. Когда администратор его проверит, придёт уведомление.',
        )
    if view.status == TaskUserStatus.REJECTED:
        return texts.t(
            'REWARD_TASKS_STATUS_REJECTED', '❌ Отчёт отклонён: {reason}\nМожно отправить новый отчёт.'
        ).format(reason=html.escape(view.reject_reason or '—'))
    if task.kind == RewardTaskKind.CHANNEL_SUBSCRIPTION:
        return texts.t(
            'REWARD_TASKS_CHANNEL_HINT',
            'Подпишитесь на канал и нажмите «Проверить подписку». Награда выдаётся один раз.',
        )
    if task.kind == RewardTaskKind.CHANNEL_BOOST:
        return texts.t(
            'REWARD_TASKS_BOOST_HINT',
            'Забустите канал, затем нажмите «Проверить буст». Нужно бустов: {required}.\n'
            'Бустить каналы могут пользователи с Telegram Premium.',
        ).format(required=task.min_boosts or rts.DEFAULT_MIN_BOOSTS)
    # Тексты с числом скриншотов построены так, чтобы не склонять существительное
    # («до 1 шт.», «Скриншотов — не больше 1»): max_photos бывает и 1, и 10.
    return texts.t(
        'REWARD_TASKS_PHOTO_HINT',
        'Выполните задание и пришлите скриншоты в подтверждение (до {max} шт.) — отчёт проверит администратор.',
    ).format(max=task.max_photos)


def _detail_text(texts: Any, view: Any) -> str:
    task = view.task
    parts = [f'{status_icon(view.status)} <b>{html.escape(task.title)}</b>']
    description = (task.description or '').strip()
    if description:
        parts.append(html.escape(description))
    parts.append(
        texts.t('REWARD_TASKS_REWARD_LINE', '🎁 Награда: <b>{reward}</b> к подписке').format(
            reward=format_reward_days(texts, task.reward_days)
        )
    )
    parts.append(_status_line(texts, view))
    return '\n\n'.join(parts)


def _photo_status_text(
    texts: Any,
    count: int,
    max_photos: int,
    *,
    overflow: bool = False,
    not_photo: bool = False,
) -> str:
    lines = [
        texts.t('REWARD_TASKS_PHOTO_STATUS', '📸 Получено скриншотов: {count} из {max}.').format(
            count=count, max=max_photos
        )
    ]
    if count <= 0:
        lines.append(
            texts.t('REWARD_TASKS_PHOTO_STATUS_EMPTY', 'Пришлите скриншоты фотографиями — по одному или альбомом.')
        )
    elif count < max_photos:
        lines.append(texts.t('REWARD_TASKS_PHOTO_STATUS_MORE', 'Можно прислать ещё или нажать «Готово».'))
    else:
        lines.append(
            texts.t('REWARD_TASKS_PHOTO_STATUS_FULL', 'Это максимум — нажмите «Готово», чтобы отправить отчёт.')
        )
    if overflow:
        lines.append(
            texts.t('REWARD_TASKS_PHOTO_OVERFLOW', '⚠️ Скриншотов — не больше {max}, лишние не добавлены.').format(
                max=max_photos
            )
        )
    if not_photo:
        lines.append(
            texts.t(
                'REWARD_TASKS_PHOTO_ONLY',
                '⚠️ Принимаются только фото. Отправьте скриншот как фото, а не файлом.',
            )
        )
    return '\n'.join(lines)


def _photo_prompt_text(texts: Any, task: Any, count: int, max_photos: int) -> str:
    """Подсказка сбора скриншотов: задание целиком (что снимать) и как прислать.

    Описание не обрезается, как и на экране задания (≤3000 символов, весь текст
    влезает в 4096): длинная подсказка в режиме логотипа не помещается в подпись
    к фото (1024), и пропатченный ``Message.edit_text`` присылает её текстом.
    """
    parts = [
        texts.t('REWARD_TASKS_PHOTO_PROMPT_TITLE', '📸 <b>Отчёт: {title}</b>').format(title=html.escape(task.title))
    ]
    description = (task.description or '').strip()
    if description:
        parts.append(html.escape(description))
    parts.append(
        texts.t(
            'REWARD_TASKS_PHOTO_PROMPT',
            'Пришлите скриншоты (до {max} шт.) — по одному или альбомом, затем нажмите «Готово».\n'
            'Подпись к фото увидит администратор, который проверяет отчёт.',
        ).format(max=max_photos)
    )
    if count:
        parts.append(_photo_status_text(texts, count, max_photos))
    return '\n\n'.join(parts)


def _task_reminder_text(texts: Any, data: dict[str, Any]) -> str | None:
    """Напоминание в статусе: название задания и начало описания (из state data)."""
    title = data.get(_TASK_TITLE_KEY)
    if not title:
        return None  # сбор начат до обновления бота — статус без напоминания
    lines = [texts.t('REWARD_TASKS_PHOTO_STATUS_TASK', '📝 Задание: <b>{title}</b>').format(title=html.escape(title))]
    excerpt = data.get(_TASK_EXCERPT_KEY)
    if excerpt:
        lines.append(html.escape(excerpt))
    return '\n'.join(lines)


def _boost_success_text(texts: Any, result: rts.RewardGrantResult) -> str:
    text = texts.t(
        'REWARD_TASKS_BOOST_SUCCESS',
        '✅ <b>Буст засчитан!</b>\n\nК подписке добавлено {reward} — спасибо за поддержку канала!',
    ).format(reward=format_reward_days(texts, result.reward_days))
    if result.end_date:
        until = texts.t('REWARD_TASKS_SUBSCRIPTION_UNTIL', 'Подписка действует до {date}.').format(
            date=format_local_datetime(result.end_date, '%d.%m.%Y')
        )
        text += '\n' + until
    return text


# ── Скрытый раздел ───────────────────────────────────────────────────────────


def _unavailable_text(language: str | None) -> str:
    return get_texts(language).t('REWARD_TASKS_UNAVAILABLE', 'Раздел недоступен')


async def _visible_access(db: AsyncSession, db_user: User) -> rts.RewardTasksAccess | None:
    """Доступ к разделу, если он виден пользователю (см. ``rts.get_reward_tasks_access``), иначе None."""
    access = await rts.get_reward_tasks_access(db, db_user)
    return access if access.visible else None


async def _clear_photo_flow(state: FSMContext, user_id: int) -> dict[str, Any] | None:
    """Под замком пользователя: сбрасывает сбор скриншотов, чужие сценарии FSM не трогает.

    Возвращает данные сбора (по ним удаляется статус) или None, если сбора не было.
    """
    if await state.get_state() != RewardTaskStates.waiting_for_photos.state:
        return None
    data = await state.get_data()
    _cancel_status(user_id)
    await state.clear()
    return data


async def _drop_photo_flow(state: FSMContext, bot: Bot, user_id: int) -> bool:
    """Сбрасывает зависший сбор скриншотов и удаляет его статус; True — сбор был.

    Под замком: из альбома, пришедшего пачкой, сбрасывает (и получает ответ)
    только первое сообщение, остальные видят пустой state.
    """
    async with _user_lock(user_id):
        data = await _clear_photo_flow(state, user_id)
    if data is None:
        return False
    await _delete_status_message(bot, data)
    return True


async def _refuse_hidden(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    *,
    message_if_expired: bool = False,
) -> None:
    """Раздел скрыт: короткий нейтральный алерт, зависший сбор отчёта сброшен — и всё.

    Так отвечают старые кнопки (меню, экран задания, кнопки под уведомлениями,
    «Проверить буст», сбор отчёта), когда раздела для пользователя нет:
    отключённая подписка или режим «только админы» (ответ владельца 2026-10-09).
    """
    await _drop_photo_flow(state, callback.bot, db_user.id)
    await _answer_alert(callback, _unavailable_text(db_user.language), message_if_expired=message_if_expired)


async def _answer_hidden(callback: types.CallbackQuery, db_user: User, cleared_flow: dict[str, Any] | None) -> None:
    """То же, что :func:`_refuse_hidden`, когда сбор уже сброшен под замком (``_clear_photo_flow``)."""
    if cleared_flow is not None:
        await _delete_status_message(callback.bot, cleared_flow)
    await _answer_alert(callback, _unavailable_text(db_user.language))


async def _photo_flow_visible(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext) -> bool:
    """Сбор скриншотов идёт, только пока раздел виден; иначе сбор сброшен и один нейтральный ответ."""
    if await _visible_access(db, db_user) is not None:
        return True
    if await _drop_photo_flow(state, message.bot, db_user.id):
        await message.answer(_unavailable_text(db_user.language))
    return False


# ── Экраны ───────────────────────────────────────────────────────────────────


async def _build_overview(
    db: AsyncSession, db_user: User, access: rts.RewardTasksAccess
) -> tuple[str, types.InlineKeyboardMarkup]:
    """Список заданий, а без права на них — экран с выходом в триал/покупку.

    ``access`` — видимый доступ (``_visible_access``): скрытому разделу экранов нет.
    """
    language = db_user.language
    texts = get_texts(language)
    overview = await rts.list_tasks_for_user(db, db_user, access=access)
    if not overview.tasks:
        return _empty_text(texts), get_back_keyboard(language)
    if not overview.eligible:
        keyboard = get_reward_tasks_ineligible_keyboard(
            language,
            overview.ineligible_reason,
            show_trial=_trial_available(db_user),
        )
        return _ineligible_text(texts, language, overview.ineligible_reason, overview.tasks), keyboard
    return _list_text(texts), get_reward_tasks_list_keyboard(language, overview.tasks)


async def _show_overview(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    access: rts.RewardTasksAccess | None = None,
) -> bool:
    """Список заданий. ``access`` не передан — считается заново; False — раздел скрыт, ничего не показано."""
    if access is None:
        access = await _visible_access(db, db_user)
        if access is None:
            return False
    text, keyboard = await _build_overview(db, db_user, access)
    await _show_screen(callback, text, keyboard)
    return True


async def _show_task_or_overview(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    task_id: int | None,
    access: rts.RewardTasksAccess | None = None,
) -> bool:
    """Экран задания; если задания нет или выполнять задания нельзя — список
    (он же экран «почему нельзя» со своими кнопками). False — раздел скрыт."""
    if access is None:
        access = await _visible_access(db, db_user)
        if access is None:
            return False
    view = await rts.get_task_view(db, db_user, task_id) if task_id is not None else None
    if view is None or access.ineligible_reason is not None:
        return await _show_overview(callback, db_user, db, access)
    texts = get_texts(db_user.language)
    await _show_screen(callback, _detail_text(texts, view), get_reward_task_detail_keyboard(db_user.language, view))
    return True


async def _show_task_error(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    error: RewardTaskError,
    task_id: int | None,
    *,
    message_if_expired: bool = False,
) -> None:
    """Ошибка действия с заданием: алерт и экран, на котором понятно, что делать дальше.

    Экран строится по свежему доступу: раздел мог скрыться посреди действия (тогда
    ``tasks_unavailable``/``subscription_disabled`` от сервиса — нейтральный
    алерт, как у скрытого раздела). ``message_if_expired`` — см. :func:`_answer_alert`.
    """
    if error.code in _HIDDEN_ERRORS:
        await _refuse_hidden(callback, db_user, state, message_if_expired=message_if_expired)
        return
    if error.code in _INELIGIBLE_ERRORS:
        if await _show_overview(callback, db_user, db):
            await callback.answer()
        else:
            await _refuse_hidden(callback, db_user, state, message_if_expired=message_if_expired)
        return
    await _answer_alert(
        callback, rts.format_error_message(error, db_user.language), message_if_expired=message_if_expired
    )
    if error.code in _LIST_ERRORS:
        await _show_overview(callback, db_user, db)
    elif error.code in _DETAIL_ERRORS:
        await _show_task_or_overview(callback, db_user, db, task_id)


def _report_entry_error(view: rts.RewardTaskView | None) -> RewardTaskError | None:
    if view is None:
        return RewardTaskError(RewardTaskErrorCode.TASK_INACTIVE)
    if view.task.kind != RewardTaskKind.PHOTO_REPORT:
        return RewardTaskError(RewardTaskErrorCode.WRONG_TASK_KIND)
    if view.status == TaskUserStatus.COMPLETED:
        return RewardTaskError(RewardTaskErrorCode.ALREADY_COMPLETED)
    if view.status == TaskUserStatus.PENDING:
        return RewardTaskError(RewardTaskErrorCode.ALREADY_PENDING)
    return None


async def _edit_prompt(
    callback: types.CallbackQuery,
    text: str,
    keyboard: types.InlineKeyboardMarkup,
) -> tuple[int, int]:
    """Экран задания становится подсказкой для отчёта; (chat_id, message_id) подсказки."""
    message = callback.message
    chat_id = message.chat.id if message is not None else callback.from_user.id
    if message is not None and not isinstance(message, InaccessibleMessage):
        result = await message.edit_text(text, reply_markup=keyboard)
        if isinstance(result, types.Message):
            # Фото-сообщение без логотипа заменяется новым — у него другой id
            return result.chat.id, result.message_id
        if result is not None:
            return chat_id, message.message_id
        # None — сообщение пропало (удалено или устарело): шлём новое
    sent = await callback.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    return chat_id, sent.message_id


async def open_reward_tasks_message(
    message: types.Message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> bool:
    """Список заданий новым сообщением: команда /tasks и диплинк /start tasks.

    False — раздел пользователю не виден (или пользователя нет): ничего не
    отправлено, вызывающий решает сам (``/start tasks`` открывает обычный
    старт, ``/tasks`` — нейтральный ответ).
    """
    if db_user is None:
        return False
    access = await _visible_access(db, db_user)
    if access is None:
        return False
    await _leave_photo_flow(state, message.bot, db_user.id)
    text, keyboard = await _build_overview(db, db_user, access)
    await message.answer(text, reply_markup=keyboard)
    return True


# ── Хендлеры ─────────────────────────────────────────────────────────────────
#
# Каждый вход сначала спрашивает rts.get_reward_tasks_access: скрытому разделу
# (отключённая подписка, режим «только админы») — нейтральный алерт или ответ,
# зависший сбор отчёта сбрасывается, больше ничего.


@error_handler
async def show_reward_tasks(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Кнопка главного меню «🎁 Задания» и «⬅️ К заданиям»."""
    access = await _visible_access(db, db_user)
    if access is None:
        await _refuse_hidden(callback, db_user, state)
        return
    await _leave_photo_flow(state, callback.bot, db_user.id, keep_message_id=_message_id(callback))
    await _show_overview(callback, db_user, db, access)
    await callback.answer()


@error_handler
async def cmd_tasks(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Команда /tasks; скрытому разделу — короткий нейтральный ответ."""
    if await open_reward_tasks_message(message, db_user, db, state) or db_user is None:
        return
    await _drop_photo_flow(state, message.bot, db_user.id)
    await message.answer(_unavailable_text(db_user.language))


@error_handler
async def show_reward_task(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Экран задания: описание, награда, статус и действие.

    Сюда ведёт и кнопка под уведомлением об отклонённом отчёте.
    """
    access = await _visible_access(db, db_user)
    if access is None:
        await _refuse_hidden(callback, db_user, state)
        return
    task_id = _parse_task_id(callback.data, TASK_CALLBACK_PREFIX)
    view = await rts.get_task_view(db, db_user, task_id) if task_id is not None else None
    if view is None:
        await _show_task_error(
            callback, db_user, db, state, RewardTaskError(RewardTaskErrorCode.TASK_INACTIVE), task_id
        )
        return
    if access.ineligible_reason is not None:
        # Без права на задания экран задания — тупик (его кнопки откажут):
        # сразу экран «почему нельзя», как и вместо списка.
        await _show_overview(callback, db_user, db, access)
        await callback.answer()
        return
    texts = get_texts(db_user.language)
    await _show_screen(callback, _detail_text(texts, view), get_reward_task_detail_keyboard(db_user.language, view))
    await callback.answer()


@error_handler
async def check_reward_task_boost(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """«🔄 Проверить буст»: проверка через getUserChatBoosts и сразу начисление.

    На нажатие отвечаем после начисления, а оно может ждать панель до ~13 с.
    Успех — экран (редактировать сообщение можно и по старому callback), ошибка —
    алерт, а если Telegram его уже не принимает, то сообщение.
    """
    if await _visible_access(db, db_user) is None:
        await _refuse_hidden(callback, db_user, state)
        return
    texts = get_texts(db_user.language)
    task_id = _parse_task_id(callback.data, BOOST_CALLBACK_PREFIX)
    try:
        if task_id is None:
            raise RewardTaskError(RewardTaskErrorCode.TASK_NOT_FOUND)
        task = await rts.require_active_task(db, task_id, kind=RewardTaskKind.CHANNEL_BOOST)
        result = await rts.check_boost(db, db_user, task, callback.bot)
    except RewardTaskError as error:
        await _show_task_error(callback, db_user, db, state, error, task_id, message_if_expired=True)
        return
    await _show_screen(callback, _boost_success_text(texts, result), get_reward_task_result_keyboard(db_user.language))
    await callback.answer()


@error_handler
async def check_reward_task_channel(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Проверяет подписку на канал и показывает результат начисления дней."""
    if await _visible_access(db, db_user) is None:
        await _refuse_hidden(callback, db_user, state)
        return
    texts = get_texts(db_user.language)
    task_id = _parse_task_id(callback.data, CHANNEL_CALLBACK_PREFIX)
    try:
        if task_id is None:
            raise RewardTaskError(RewardTaskErrorCode.TASK_NOT_FOUND)
        task = await rts.require_active_task(db, task_id, kind=RewardTaskKind.CHANNEL_SUBSCRIPTION)
        result = await rts.check_channel_subscription(db, db_user, task, callback.bot)
    except RewardTaskError as error:
        await _show_task_error(callback, db_user, db, state, error, task_id, message_if_expired=True)
        return
    await _show_screen(callback, _boost_success_text(texts, result), get_reward_task_result_keyboard(db_user.language))
    await callback.answer()


@error_handler
async def start_reward_task_report(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """«📸 Отправить отчёт»: вход в сбор скриншотов.

    Повторный вход в то же задание (кнопка со старого экрана) сохраняет уже
    присланные фото; вход в другое задание начинает сбор заново.
    """
    access = await _visible_access(db, db_user)
    if access is None:
        await _refuse_hidden(callback, db_user, state)
        return
    texts = get_texts(db_user.language)
    task_id = _parse_task_id(callback.data, PHOTO_CALLBACK_PREFIX)
    view = await rts.get_task_view(db, db_user, task_id) if task_id is not None else None
    error = _report_entry_error(view)
    if error is None and access.ineligible_reason is not None:
        error = RewardTaskError(access.ineligible_reason)
    if error is not None:
        await _show_task_error(callback, db_user, db, state, error, task_id)
        return

    task = view.task
    async with _user_lock(db_user.id):
        current_state = await state.get_state()
        data = await state.get_data()
        in_flow = current_state == RewardTaskStates.waiting_for_photos.state
        same_task = in_flow and data.get(_TASK_ID_KEY) == task.id
        max_photos = task.max_photos
        photos = list(data.get(_PHOTOS_KEY) or [])[:max_photos] if same_task else []
        _cancel_status(db_user.id)

        text = _photo_prompt_text(texts, task, len(photos), max_photos)
        keyboard = get_reward_task_photo_keyboard(db_user.language, task.id, len(photos))
        chat_id, message_id = await _edit_prompt(callback, text, keyboard)

        await state.set_state(RewardTaskStates.waiting_for_photos)
        await state.set_data(
            {
                _TASK_ID_KEY: task.id,
                # Для напоминания в статусе: его публикует фоновая задача без БД
                _TASK_TITLE_KEY: task.title,
                _TASK_EXCERPT_KEY: shorten(task.description or '', STATUS_EXCERPT_LIMIT) or None,
                _MAX_PHOTOS_KEY: max_photos,
                _PHOTOS_KEY: photos,
                _COMMENT_KEY: data.get(_COMMENT_KEY) if same_task else None,
                _STATUS_CHAT_KEY: chat_id,
                _STATUS_MESSAGE_KEY: message_id,
            }
        )
    if in_flow:
        await _delete_status_message(callback.bot, data, keep_message_id=message_id)
    await callback.answer()


@error_handler
async def collect_reward_task_photo(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    """Скриншот для отчёта — одиночное фото или одно сообщение из альбома."""
    item = rts.build_photo_media_item(message.photo or [])
    if item is None or db_user is None:
        return
    if not await _photo_flow_visible(message, db_user, db, state):
        return
    async with _user_lock(db_user.id):
        if await state.get_state() != RewardTaskStates.waiting_for_photos.state:
            # «Готово» или «Отмена» успели раньше, чем дошла очередь до этого фото
            return
        data = await state.get_data()
        photos = list(data.get(_PHOTOS_KEY) or [])
        updates: dict[str, Any] = {}
        if _contains_photo(photos, item):
            pass  # тот же файл ещё раз (повторная пересылка)
        elif len(photos) >= _max_photos(data):
            updates[_OVERFLOW_KEY] = True
        else:
            photos.append(item)
            updates[_PHOTOS_KEY] = photos
            comment = _merge_comment(data.get(_COMMENT_KEY), message.caption)
            if comment != data.get(_COMMENT_KEY):
                updates[_COMMENT_KEY] = comment
        if updates:
            await state.update_data(updates)
    _schedule_status(message.bot, user_id=db_user.id, chat_id=message.chat.id, state=state, language=db_user.language)


@error_handler
async def remind_photo_only(message: types.Message, db_user: User, state: FSMContext, db: AsyncSession):
    """Текст, файл или стикер вместо фото — подсказка в ближайшем статусе."""
    if db_user is None:
        return
    if not await _photo_flow_visible(message, db_user, db, state):
        return
    async with _user_lock(db_user.id):
        if await state.get_state() != RewardTaskStates.waiting_for_photos.state:
            return
        await state.update_data({_NOT_PHOTO_KEY: True})
    _schedule_status(message.bot, user_id=db_user.id, chat_id=message.chat.id, state=state, language=db_user.language)


@error_handler
async def submit_reward_task_report(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """«Готово»: отчёт уходит на проверку (source='bot').

    Видимость раздела проверяется уже под замком: «Готово» должно опередить
    отложенный статус (его публикация тоже ждёт замка), как и до этой проверки.
    """
    texts = get_texts(db_user.language)
    task_id = _parse_task_id(callback.data, PHOTO_DONE_CALLBACK_PREFIX)
    failure: RewardTaskError | None = None
    hidden_flow: dict[str, Any] | None = None
    async with _user_lock(db_user.id):
        access = await _visible_access(db, db_user)
        data = await state.get_data()
        if access is None:
            hidden_flow = await _clear_photo_flow(state, db_user.id)
        elif not await _is_collecting(state, data, task_id):
            await _answer_alert(
                callback, texts.t('REWARD_TASKS_REPORT_EXPIRED', 'Этот отчёт уже отправлен или отменён.')
            )
            await _show_task_or_overview(callback, db_user, db, task_id, access)
            return
        else:
            photos = list(data.get(_PHOTOS_KEY) or [])
            if not photos:
                await _answer_alert(
                    callback, rts.format_error_message(RewardTaskErrorCode.MEDIA_REQUIRED, db_user.language)
                )
                return
            try:
                task = await rts.require_active_task(db, task_id, kind=RewardTaskKind.PHOTO_REPORT)
                await rts.submit_photo_report(
                    db,
                    db_user,
                    task,
                    photos,
                    comment=data.get(_COMMENT_KEY),
                    source=RewardTaskSubmissionSource.BOT,
                )
            except RewardTaskError as error:
                failure = error
            if failure is not None and failure.code in _MEDIA_ERRORS:
                # Собрать заново: скриншоты сброшены, сценарий продолжается, свежий
                # статус (0 из N) придёт обычным путём.
                await state.update_data(
                    {
                        _PHOTOS_KEY: [],
                        _COMMENT_KEY: None,
                        _MAX_PHOTOS_KEY: int(failure.params.get('max') or _max_photos(data)),
                        _OVERFLOW_KEY: False,
                    }
                )
                await _answer_alert(
                    callback,
                    rts.format_error_message(failure, db_user.language)
                    + ' '
                    + texts.t('REWARD_TASKS_PHOTOS_RESET', 'Пришлите скриншоты заново.'),
                )
                _schedule_status(
                    callback.bot,
                    user_id=db_user.id,
                    chat_id=data.get(_STATUS_CHAT_KEY) or callback.from_user.id,
                    state=state,
                    language=db_user.language,
                )
                return
            _cancel_status(db_user.id)
            await state.clear()

    if access is None:
        await _answer_hidden(callback, db_user, hidden_flow)
        return
    await _delete_status_message(callback.bot, data, keep_message_id=_message_id(callback))
    if failure is not None:
        await _show_task_error(callback, db_user, db, state, failure, task_id)
        return
    await _show_screen(
        callback,
        texts.t(
            'REWARD_TASKS_REPORT_SENT',
            '✅ <b>Отчёт отправлен на проверку</b>\n\n'
            'Администратор проверит скриншоты, а мы пришлём уведомление с результатом.',
        ),
        get_reward_task_result_keyboard(db_user.language),
    )
    await callback.answer()


@error_handler
async def cancel_reward_task_report(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    """«Отмена»: выход из сбора скриншотов обратно к заданию (видимость — под замком, как у «Готово»)."""
    texts = get_texts(db_user.language)
    task_id = _parse_task_id(callback.data, PHOTO_CANCEL_CALLBACK_PREFIX)
    cancelled = False
    hidden_flow: dict[str, Any] | None = None
    async with _user_lock(db_user.id):
        access = await _visible_access(db, db_user)
        data = await state.get_data()
        if access is None:
            hidden_flow = await _clear_photo_flow(state, db_user.id)
        elif await _is_collecting(state, data, task_id):
            _cancel_status(db_user.id)
            await state.clear()
            cancelled = True
    if access is None:
        await _answer_hidden(callback, db_user, hidden_flow)
        return
    if cancelled:
        await _delete_status_message(callback.bot, data, keep_message_id=_message_id(callback))
    await _show_task_or_overview(callback, db_user, db, task_id, access)
    if cancelled:
        await callback.answer(texts.t('REWARD_TASKS_REPORT_CANCELLED', 'Отправка отчёта отменена.'))
    else:
        await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_reward_tasks, F.data == REWARD_TASKS_MENU_CALLBACK)
    dp.callback_query.register(show_reward_task, F.data.startswith(TASK_CALLBACK_PREFIX))
    dp.callback_query.register(check_reward_task_channel, F.data.startswith(CHANNEL_CALLBACK_PREFIX))
    dp.callback_query.register(check_reward_task_boost, F.data.startswith(BOOST_CALLBACK_PREFIX))
    dp.callback_query.register(start_reward_task_report, F.data.startswith(PHOTO_CALLBACK_PREFIX))
    dp.callback_query.register(submit_reward_task_report, F.data.startswith(PHOTO_DONE_CALLBACK_PREFIX))
    dp.callback_query.register(cancel_reward_task_report, F.data.startswith(PHOTO_CANCEL_CALLBACK_PREFIX))

    # Команда — раньше хендлеров сбора: /tasks посреди отчёта открывает список,
    # а не подсказку «пришлите фото».
    dp.message.register(cmd_tasks, Command('tasks'))
    dp.message.register(collect_reward_task_photo, RewardTaskStates.waiting_for_photos, F.photo)
    dp.message.register(remind_photo_only, RewardTaskStates.waiting_for_photos)
