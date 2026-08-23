"""Донат-апселл: «добавить 10 ₽ к платежу» при пополнении баланса.

Единственный источник правды для бота и кабинета: показывать ли предложение,
что именно предлагать, как зафиксировать решение и как исполнить обещание после
оплаты. Механика денег описана в
``docs/superpowers/specs/2026-07-30-donation-upsell-design.md``:

  платёж на (пополнение + надбавка) → вебхук зачисляет всю сумму
  → ``try_apply_donation_pledge`` списывает надбавку и пишет пожертвование.

Порядок «зачислить всё, потом списать надбавку» выбран сознательно: баланс не
уходит в минус, а любая ошибка исполнения оставляет деньги пользователю.
"""

from dataclasses import dataclass

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import donation_pledge as pledge_crud
from app.database.crud.donation import create_donation, get_user_donation_total_kopeks
from app.database.crud.transaction import create_transaction
from app.database.crud.user import lock_user_for_update
from app.database.models import (
    DonationPledge,
    DonationPledgeStatus,
    PaymentMethod,
    TransactionType,
    User,
)
from app.localization.texts import get_texts


logger = structlog.get_logger(__name__)

# Способы, для которых обещание заведомо неисполнимо, — апселл им не показываем.
#
# tribute: сумму платежа выбирает пользователь на стороне Tribute
#   (app/handlers/balance/tribute.py передаёт amount_kopeks=0), задать
#   «пополнение + надбавка» локально нечем.
# cryptobot: инвойс выставляется в USD (amount_usd = рубли / курс), а при оплате
#   рублёвая сумма пересчитывается по курсу НА МОМЕНТ ОПЛАТЫ
#   (app/services/payment/cryptobot.py). За сутки жизни инвойса курс уходит на
#   проценты — это десятки рублей, тогда как окно сверки меньше надбавки
#   (см. _match_tolerance_kopeks). Обещание не исполнилось бы почти никогда, зато
#   висело бы pending весь TTL. Честнее не предлагать, чем предложить и не сделать.
EXCLUDED_UPSELL_METHODS = frozenset({'tribute', 'cryptobot'})

# Предохранитель на «иную сумму»: 50 000 ₽ надбавки — заведомо ошибка ввода.
MAX_TIP_KOPEKS = 5_000_000

DONATION_UPSELL_SOURCE = 'topup_upsell'


@dataclass(frozen=True)
class UpsellOffer:
    """Что показать пользователю на экране апселла."""

    tip_kopeks: int
    presets_kopeks: list[int]
    topup_amount_kopeks: int
    total_kopeks: int
    max_tip_kopeks: int


def resolve_max_tip_kopeks(*, topup_amount_kopeks: int, method_max_kopeks: int | None) -> int:
    """Наибольшая надбавка, которая ещё влезает в лимит способа оплаты."""
    limit = MAX_TIP_KOPEKS
    if method_max_kopeks is not None:
        limit = min(limit, max(0, int(method_max_kopeks) - int(topup_amount_kopeks)))
    return max(0, limit)


# Допуск при сверке суммы. Расхождение реально бывает у CryptoBot: сумма в рублях
# пересчитывается по живому курсу USD→RUB в момент оплаты (payment/cryptobot.py).
# Потолок в 2 ₽ — но он ВСЕГДА сжимается до величины меньше самой надбавки, см.
# _match_tolerance_kopeks: иначе обычное пополнение на базовую сумму попадало бы в
# окно забытого обещания и списывало бы с человека чужую надбавку.
PLEDGE_MATCH_TOLERANCE_KOPEKS = 200


def _match_tolerance_kopeks(tip_kopeks: int) -> int:
    """Окно сверки суммы — строго уже надбавки.

    Инвариант ``tolerance < tip_kopeks`` — единственное, что отделяет «платёж по
    обещанию» от «обычного пополнения на ту же базовую сумму»: между ними разница
    ровно в надбавку.
    """
    return max(0, min(PLEDGE_MATCH_TOLERANCE_KOPEKS, int(tip_kopeks) - 1))


def pledge_external_id(pledge_id: int) -> str:
    """Идентификатор пожертвования-надбавки. Префикс — контракт для истории баланса."""
    return f'pledge:{pledge_id}'


def select_matching_pledge(
    pledges: list[DonationPledge],
    *,
    credited_amount_kopeks: int,
) -> DonationPledge | None:
    """Обещание, к которому относится зачисленный платёж (самое свежее подходящее).

    ``pledges`` приходит отсортированным «новые первыми». Сверяем сумму: без этого
    брошенный платёж мог бы списать надбавку со следующего пополнения.
    """
    for pledge in pledges:
        tip = int(getattr(pledge, 'tip_kopeks', 0) or 0)
        if tip <= 0:
            continue
        expected = int(getattr(pledge, 'expected_total_kopeks', 0) or 0)
        if abs(credited_amount_kopeks - expected) <= _match_tolerance_kopeks(tip):
            return pledge
    return None


async def peek_pending_tip_kopeks(
    db: AsyncSession,
    *,
    user_id: int,
    credited_amount_kopeks: int,
) -> int:
    """Надбавка, которую вот-вот спишет ``try_apply_donation_pledge`` (0 — списывать нечего).

    Взгляд вперёд только на чтение — для расчётов, которые провайдер делает ДО
    пост-хука. Такой расчёт один: реферальная комиссия
    (``app/services/referral_service.py::process_referral_topup``). В момент её
    начисления надбавка ещё лежит на балансе, поэтому без поправки реферер получал
    бы процент и с чужого пожертвования.

    Матчер тот же, что и у применения: иначе стороны могли бы разойтись в решении —
    комиссию урезали бы, а надбавку не списали. Любая ошибка молча даёт 0, то есть
    прежнее поведение: недобрать комиссию хуже, чем уронить зачисление платежа.
    """
    if db is None:
        return 0
    if not settings.DONATIONS_ENABLED or not settings.DONATION_UPSELL_ENABLED:
        return 0

    try:
        pledges = await pledge_crud.list_pending_pledges(db, user_id=user_id)
        pledge = select_matching_pledge(pledges, credited_amount_kopeks=credited_amount_kopeks)
    except Exception as error:  # pragma: no cover — БД недоступна
        logger.warning('Не удалось заглянуть в обещание донат-апселла', user_id=user_id, error=error)
        return 0

    if pledge is None:
        return 0
    return max(0, int(getattr(pledge, 'tip_kopeks', 0) or 0))


async def applied_tip_for_credited_amount(
    db: AsyncSession,
    *,
    user_id: int,
    credited_amount_kopeks: int,
) -> int:
    """Надбавка, уже списанная с давнего пополнения (0 — её там не было).

    Транзакция ``DEPOSIT`` хранит полную сумму платежа: надбавка ушла отдельной
    транзакцией ``DONATION`` уже после зачисления. Разбирая историю задним числом
    (``app/services/referral_diagnostics_service.py``), надо считать так же, как
    считал живой путь, — иначе ремонт доначислит то, чего живой путь сознательно
    не платил. Матчер общий, ошибки молча дают 0.

    Флаги фичи здесь намеренно не проверяются: надбавка в истории уже списана, и
    выключение апселла задним числом её не отменяет.
    """
    if db is None:
        return 0

    try:
        pledges = await pledge_crud.list_applied_pledges(db, user_id=user_id)
        pledge = select_matching_pledge(pledges, credited_amount_kopeks=credited_amount_kopeks)
    except Exception as error:  # pragma: no cover — БД недоступна
        logger.warning('Не удалось найти применённую надбавку', user_id=user_id, error=error)
        return 0

    if pledge is None:
        return 0
    return max(0, int(getattr(pledge, 'tip_kopeks', 0) or 0))


async def is_upsell_available(db: AsyncSession, user: User) -> bool:
    """Условия, не зависящие от суммы и способа оплаты (для кабинета и бота)."""
    if not settings.DONATIONS_ENABLED or not settings.DONATION_UPSELL_ENABLED:
        return False
    if getattr(user, 'restriction_topup', False):
        return False
    if settings.get_donation_upsell_tip_kopeks() <= 0:
        return False

    if await pledge_crud.has_recent_decision(db, user_id=user.id, days=settings.DONATION_UPSELL_COOLDOWN_DAYS):
        return False

    if settings.DONATION_UPSELL_SKIP_IF_DONATED:
        try:
            donated = await get_user_donation_total_kopeks(db, user.id)
        except Exception as error:  # pragma: no cover — статистика не должна ломать оплату
            logger.warning('Не удалось проверить прошлые пожертвования', user_id=user.id, error=error)
            donated = 0
        if donated > 0:
            return False

    return True


async def build_offer(
    db: AsyncSession,
    user: User,
    *,
    topup_amount_kopeks: int,
    payment_method: str,
    method_max_kopeks: int | None = None,
) -> UpsellOffer | None:
    """Предложение для конкретного пополнения, либо ``None`` — не показывать."""
    if payment_method in EXCLUDED_UPSELL_METHODS:
        return None
    if topup_amount_kopeks < settings.get_donation_upsell_min_topup_kopeks():
        return None
    if not await is_upsell_available(db, user):
        return None

    tip_kopeks = settings.get_donation_upsell_tip_kopeks()
    max_tip = resolve_max_tip_kopeks(topup_amount_kopeks=topup_amount_kopeks, method_max_kopeks=method_max_kopeks)
    if tip_kopeks > max_tip:
        # Пополнение уже у верхней границы способа — надбавку провайдер не примет.
        return None

    presets = [amount for amount in settings.get_donation_upsell_presets_kopeks() if amount <= max_tip]

    return UpsellOffer(
        tip_kopeks=tip_kopeks,
        presets_kopeks=presets,
        topup_amount_kopeks=topup_amount_kopeks,
        total_kopeks=topup_amount_kopeks + tip_kopeks,
        max_tip_kopeks=max_tip,
    )


async def register_pledge(
    db: AsyncSession,
    user: User,
    *,
    tip_kopeks: int,
    topup_amount_kopeks: int,
    payment_method: str | None,
    source: str,
) -> DonationPledge:
    """Фиксирует согласие: гасит прежние обещания и создаёт новое ``pending``."""
    await pledge_crud.supersede_pending_pledges(db, user_id=user.id)
    return await pledge_crud.create_pledge(
        db,
        user_id=user.id,
        tip_kopeks=tip_kopeks,
        topup_amount_kopeks=topup_amount_kopeks,
        payment_method=payment_method,
        source=source,
        ttl_hours=settings.DONATION_UPSELL_PLEDGE_TTL_HOURS,
    )


async def register_decline(
    db: AsyncSession,
    user: User,
    *,
    topup_amount_kopeks: int,
    payment_method: str | None,
    source: str,
) -> None:
    """Фиксирует отказ: нужен для cooldown и воронки, гасит прежние обещания."""
    try:
        await pledge_crud.supersede_pending_pledges(db, user_id=user.id)
        await pledge_crud.create_pledge(
            db,
            user_id=user.id,
            tip_kopeks=0,
            topup_amount_kopeks=topup_amount_kopeks,
            payment_method=payment_method,
            source=source,
            ttl_hours=settings.DONATION_UPSELL_PLEDGE_TTL_HOURS,
            status=DonationPledgeStatus.DECLINED,
        )
    except Exception as error:  # pragma: no cover — отказ не должен мешать оплате
        logger.warning('Не удалось записать отказ от донат-апселла', user_id=user.id, error=error)


async def cancel_pending_pledges(db: AsyncSession, user: User) -> int:
    """Гасит висящие обещания: человек начал пополнение, а экран апселла не показан.

    Второй предохранитель к TTL. Обещание живёт до оплаты и подбирается по сумме, а
    не по способу, поэтому забытое согласие может совпасть со следующим пополнением
    и списать надбавку, которую к тому платежу не обещали. Пока экран показывается
    при каждом пополнении, прежнее обещание гасит новое решение
    (``register_pledge``/``register_decline``) — но экрана может и не быть: cooldown,
    порог суммы, исключённый способ (Tribute/CryptoBot), лимит способа, запрет
    пополнений. Все эти случаи означают одно: начат НОВЫЙ платёж, старое обещание к
    нему не относится.

    Цена — потерянная надбавка, если человек всё-таки оплатит прежний счёт: деньги
    останутся у него на балансе. Это дешевле молчаливого списания.
    """
    if db is None:
        return 0
    try:
        cancelled = await pledge_crud.supersede_pending_pledges(db, user_id=user.id)
        if cancelled:
            await db.commit()
            logger.info('Обещания донат-апселла погашены новым пополнением', user_id=user.id, count=cancelled)
        return cancelled
    except Exception as error:  # pragma: no cover — гашение не должно ломать оплату
        # Откат обязателен: db — сессия вызывающего, и после сорванного коммита она
        # остаётся в PendingRollbackError, то есть следующий же запрос провайдера
        # (создание платежа) упал бы. Тот же приём и по той же причине — в
        # _rollback_and_revive. expire_on_commit=False, поэтому объекты сессии
        # переживают откат и refresh не нужен.
        try:
            await db.rollback()
        except Exception as rollback_error:  # pragma: no cover — сессия уже мертва
            logger.warning('Не удалось откатить сессию после гашения обещаний', error=rollback_error)
        logger.warning('Не удалось погасить прежние обещания донат-апселла', user_id=user.id, error=error)
        return 0


async def try_apply_donation_pledge(
    db: AsyncSession,
    user: User,
    credited_amount_kopeks: int,
    bot: object | None = None,
) -> int:
    """Списывает обещанную надбавку с только что пополненного баланса.

    Вызывается сразу после зачисления платежа (см.
    ``app/services/payment/common.py::send_cart_notification_after_topup``), то есть
    уже вне транзакции зачисления. Возвращает размер списанной надбавки в копейках
    (0 — ничего не списано): вызывающему это нужно, чтобы показать пользователю
    сумму, которая реально осталась на балансе.

    Ошибки намеренно проглатываются: пополнение важнее надбавки, а неисполненное
    обещание означает лишь, что деньги остались на балансе пользователя.
    """
    if not settings.DONATIONS_ENABLED or not settings.DONATION_UPSELL_ENABLED:
        return 0
    if db is None:
        # Хук вызывают и без сессии (ручные сценарии) — молча выходим, а не шумим.
        return 0

    try:
        pledges = await pledge_crud.list_pending_pledges(db, user_id=user.id)
    except Exception as error:  # pragma: no cover — БД недоступна
        logger.warning('Не удалось получить обещания донат-апселла', user_id=user.id, error=error)
        return 0

    pledge = select_matching_pledge(pledges, credited_amount_kopeks=credited_amount_kopeks)
    if pledge is None:
        if pledges:
            # Обещания есть, но сумма не сошлась. Без этой строки расхождение было бы
            # невидимым: человек согласился на надбавку, а пожертвования не появилось.
            logger.info(
                'Обещание донат-апселла есть, но сумма платежа не совпала',
                user_id=user.id,
                credited_amount_kopeks=credited_amount_kopeks,
                expected=[int(p.expected_total_kopeks or 0) for p in pledges],
            )
        return 0

    pledge_id = int(pledge.id)
    tip_kopeks = int(pledge.tip_kopeks)
    payment_method_value = pledge.payment_method
    external_id = pledge_external_id(pledge_id)

    try:
        locked_user = await lock_user_for_update(db, user)

        if locked_user.balance_kopeks < tip_kopeks:
            # Успел потратить пополнение до нашего списания — не загоняем баланс в минус.
            await pledge_crud.mark_pledge_status(db, pledge_id=pledge_id, status=DonationPledgeStatus.SKIPPED)
            await db.commit()
            logger.warning(
                'Надбавку донат-апселла применить нечем: не хватает баланса',
                user_id=user.id,
                pledge_id=pledge_id,
                tip_kopeks=tip_kopeks,
                balance_kopeks=locked_user.balance_kopeks,
            )
            return 0

        transaction = await create_transaction(
            db=db,
            user_id=locked_user.id,
            type=TransactionType.DONATION,
            amount_kopeks=tip_kopeks,
            description='Пожертвование при пополнении баланса',
            payment_method=_as_payment_method(payment_method_value),
            external_id=external_id,
            is_completed=True,
            commit=False,
        )

        # Отметка и списание — в одной транзакции: условие status='pending' внутри
        # UPDATE делает повторный вебхук безопасным (вторая попытка увидит applied).
        marked = await pledge_crud.mark_pledge_applied(db, pledge_id=pledge_id, transaction_id=transaction.id)
        if not marked:
            await _rollback_and_revive(db, user)
            logger.info('Надбавка донат-апселла уже применена, пропускаем', pledge_id=pledge_id)
            return 0

        locked_user.balance_kopeks -= tip_kopeks

        await create_donation(
            db,
            user=locked_user,
            user_id=locked_user.id,
            amount_kopeks=tip_kopeks,
            payment_method=payment_method_value,
            external_id=external_id,
            transaction_id=transaction.id,
            source=DONATION_UPSELL_SOURCE,
            commit=False,
        )

        await db.commit()
    except IntegrityError:
        # Уникальный индекс (external_id, payment_method) на transactions/donations:
        # повторная доставка вебхука — штатная ситуация, а не сбой.
        await _rollback_and_revive(db, user)
        logger.info('Надбавка донат-апселла уже записана, пропускаем', pledge_id=pledge_id)
        return 0
    except Exception as error:
        await _rollback_and_revive(db, user)
        logger.error(
            'Ошибка применения надбавки донат-апселла',
            user_id=user.id,
            pledge_id=pledge_id,
            error=error,
            exc_info=True,
        )
        return 0

    logger.info(
        '❤️ Надбавка донат-апселла записана как пожертвование',
        user_id=user.id,
        pledge_id=pledge_id,
        tip_kopeks=tip_kopeks,
        credited_amount_kopeks=credited_amount_kopeks,
    )

    if bot is not None:
        await _notify_pledge_applied(
            bot,
            db,
            user,
            tip_kopeks,
            payment_method_value,
            credited_kopeks=max(0, credited_amount_kopeks - tip_kopeks),
        )

    return tip_kopeks


async def _rollback_and_revive(db: AsyncSession, user: User) -> None:
    """Откатывает нашу неудачную попытку и «оживляет» объект пользователя.

    ``db`` — сессия ВЫЗЫВАЮЩЕГО провайдера, и его ``user`` живёт в той же identity
    map. Голый ``rollback()`` экспайрит все инстансы сессии даже при
    ``expire_on_commit=False``, после чего первый же sync-доступ к ``user.telegram_id``
    в ``send_cart_notification_after_topup`` дёргает ленивую подгрузку вне greenlet →
    MissingGreenlet, и остаток пост-хука (письмо, автовыкуп корзины, автопродление)
    не выполняется. То есть провал необязательной надбавки ронял бы основной сценарий:
    человек заплатил и не получил подписку.

    Тот же приём и по той же причине — в ``app/services/campaign_service.py``
    (прод-репорт 2026-07-13).
    """
    await db.rollback()
    try:
        await db.refresh(user)
    except Exception as refresh_error:  # pragma: no cover — сессия уже нездорова
        logger.warning(
            'Не удалось обновить пользователя после отката надбавки',
            user_id=user.__dict__.get('id'),
            error=refresh_error,
        )


def _as_payment_method(value: str | None) -> PaymentMethod | None:
    """Строка способа оплаты → enum, если он там есть (иначе None)."""
    if not value:
        return None
    try:
        return PaymentMethod(value)
    except ValueError:
        return None


async def _notify_pledge_applied(
    bot: object,
    db: AsyncSession,
    user: User,
    tip_kopeks: int,
    payment_method_value: str | None,
    credited_kopeks: int = 0,
) -> None:
    """Спасибо донору и уведомление админам. Никогда не ломает основной поток."""
    try:
        from app.services.admin_notification_service import AdminNotificationService

        await AdminNotificationService(bot).send_donation_notification(
            user,
            amount_kopeks=tip_kopeks,
            payment_method=payment_method_value,
            source=DONATION_UPSELL_SOURCE,
        )
    except Exception as error:
        logger.warning('Не удалось отправить админ-уведомление о надбавке', error=error)

    telegram_id = getattr(user, 'telegram_id', None)
    if not telegram_id:
        return

    try:
        texts = get_texts(getattr(user, 'language', None) or 'ru')
        # Сумму называем обе: провайдер уже прислал чек на полный платёж, и без второй
        # цифры человек видит «оплачено 510 ₽» при балансе 500 ₽ и не понимает, куда делись 10.
        message = texts.t(
            'DONATION_UPSELL_THANK_YOU',
            '❤️ <b>Спасибо за поддержку!</b>\n\n'
            '{amount} из вашего платежа пошли на развитие проекта — на мощности и скорость соединения. '
            'На баланс зачислено {credited}. Спасибо! 🙏',
        ).format(amount=settings.format_price(tip_kopeks), credited=settings.format_price(credited_kopeks))
        await bot.send_message(chat_id=telegram_id, text=message, parse_mode='HTML')
    except Exception as error:
        logger.warning('Не удалось поблагодарить за надбавку', error=error)
