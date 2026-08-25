"""
Блок «Пожертвования» в админском отчёте.

БД не нужна: сессия и CRUD подменяются моками. Проверяем раскладку данных CRUD в
_collect_donation_stats, состав секции в готовом тексте, гейт DONATIONS_ENABLED и то,
что секция переживает конвертацию в rich-формат и не рвёт лимит Telegram.
"""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.services import reporting_service as reporting
from app.utils.rich_admin import classic_admin_html_to_rich


START_UTC = datetime(2026, 8, 22, 21, 0, tzinfo=UTC)
END_UTC = START_UTC + timedelta(days=1)

# Реальные цифры прода: надбавки 195 шт / 2260 ₽, кнопка «Поддержать проект» 9 шт / 816 ₽.
PROD_BY_SOURCE = {
    'menu': {'amount_kopeks': 81600, 'count': 9},
    'topup_upsell': {'amount_kopeks': 226000, 'count': 195},
}


def _patch_crud(
    monkeypatch,
    *,
    total_amount: int = 307600,
    total_count: int = 204,
    donors_count: int = 87,
    by_source: dict | None = None,
) -> dict[str, AsyncMock]:
    mocks = {
        'get_total_donations_kopeks': AsyncMock(return_value=total_amount),
        'count_donations': AsyncMock(return_value=total_count),
        'count_distinct_donors': AsyncMock(return_value=donors_count),
        'get_donations_by_source': AsyncMock(return_value=PROD_BY_SOURCE if by_source is None else by_source),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(reporting, name, mock)
    return mocks


async def test_collect_donation_stats_maps_crud_payload(monkeypatch):
    mocks = _patch_crud(monkeypatch)
    service = reporting.ReportingService()

    stats = await service._collect_donation_stats(object(), START_UTC, END_UTC)

    assert stats['total_amount'] == 307600
    assert stats['total_count'] == 204
    assert stats['donors_count'] == 87
    assert stats['menu_amount'] == 81600
    assert stats['menu_count'] == 9
    assert stats['upsell_amount'] == 226000
    assert stats['upsell_count'] == 195
    # Сумма запрашивается только за период: строки «за всё время» в отчёте нет
    assert mocks['get_total_donations_kopeks'].await_count == 1
    assert mocks['get_total_donations_kopeks'].await_args.kwargs['start'] == START_UTC


async def test_collect_donation_stats_uses_half_open_period(monkeypatch):
    """Границы отчёта — [start, end), а CRUD фильтрует включительно: конец сдвигаем на микросекунду."""
    mocks = _patch_crud(monkeypatch)
    service = reporting.ReportingService()

    await service._collect_donation_stats(object(), START_UTC, END_UTC)

    kwargs = mocks['count_donations'].await_args.kwargs
    assert kwargs['start'] == START_UTC
    assert kwargs['end'] == END_UTC - timedelta(microseconds=1)


async def test_collect_donation_stats_empty_period(monkeypatch):
    _patch_crud(monkeypatch, total_amount=0, total_count=0, donors_count=0, by_source={})
    service = reporting.ReportingService()

    stats = await service._collect_donation_stats(object(), START_UTC, END_UTC)

    assert stats['total_amount'] == 0
    assert stats['total_count'] == 0
    assert stats['menu_amount'] == 0
    assert stats['upsell_count'] == 0
    assert stats['other_amount'] == 0
    assert stats['other_count'] == 0


def _donation_stats(**overrides) -> dict:
    stats = {
        'total_amount': 307600,
        'total_count': 204,
        'donors_count': 87,
        'menu_amount': 81600,
        'menu_count': 9,
        'upsell_amount': 226000,
        'upsell_count': 195,
        'other_amount': 0,
        'other_count': 0,
    }
    stats.update(overrides)
    return stats


async def _render_report(
    monkeypatch,
    *,
    donations_enabled: bool = True,
    donations: dict | None = None,
    top_referrers: list[dict] | None = None,
) -> str:
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', donations_enabled, raising=False)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=object())
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(reporting, 'AsyncSessionLocal', MagicMock(return_value=session_ctx))

    service = reporting.ReportingService()
    monkeypatch.setattr(
        service,
        '_collect_current_totals',
        AsyncMock(return_value={'active_trials': 12, 'active_paid': 340, 'open_tickets': 3}),
    )
    monkeypatch.setattr(
        service,
        '_collect_period_stats',
        AsyncMock(
            return_value={
                'new_users': 41,
                'new_trials': 30,
                'new_paid_subscriptions': 12,
                'trial_to_paid_conversions': 6,
                'subscription_payments_count': 18,
                'subscription_payments_amount': 950000,
                'deposits_count': 52,
                'deposits_amount': 1480000,
                'new_tickets': 4,
            }
        ),
    )
    monkeypatch.setattr(
        service,
        '_get_top_referrers',
        AsyncMock(
            return_value=top_referrers
            if top_referrers is not None
            else [{'referrer_label': f'@ref{index}', 'count': 10 - index} for index in range(1, 6)]
        ),
    )
    monkeypatch.setattr(
        service,
        '_get_user_usage_stats',
        AsyncMock(return_value={'active_paid_users': 320, 'never_connected_users': 15}),
    )
    donation_collector = AsyncMock(
        return_value=_donation_stats() if donations is None else donations,
        side_effect=None if donations_enabled else AssertionError('CRUD не должен вызываться при выключенных донатах'),
    )
    monkeypatch.setattr(service, '_collect_donation_stats', donation_collector)

    text = await service._build_report(reporting.ReportPeriod.DAILY, date(2026, 8, 23))
    return text, donation_collector


async def test_report_contains_donations_section(monkeypatch):
    text, _ = await _render_report(monkeypatch)

    assert '💜 <b>Пожертвования</b>' in text
    assert '• Собрано за период: <b>3 076.00 ₽</b> (донатов: 204, донатеров: 87)' in text
    assert '• Кнопка «Поддержать проект»: 816.00 ₽ (9)' in text
    assert '• Надбавка при пополнении: 2 260.00 ₽ (195)' in text
    # Хвост секции вырезан: средний чек, воронка апселла, топ спонсоров, «за всё время» и оговорка
    for removed in ('Средний донат', 'Апселл', 'Топ спонсоров', 'за всё время', 'уже входят в «Пополнения»'):
        assert removed not in text
    # TL;DR — строка последняя в блоке «Итог по периоду»
    assert '• Пожертвования: <b>3 076.00 ₽</b> <i>(из них 2 260.00 ₽ — надбавки, уже в поступлениях)</i>\n\n' in text
    # Секция стоит сразу после «Финансов», до «Поддержки»
    assert text.index('💰 <b>Финансы</b>') < text.index('💜 <b>Пожертвования</b>') < text.index('🎟️ <b>Поддержка</b>')


async def test_report_escapes_user_names(monkeypatch):
    """Имя из Telegram — чужой ввод: разметка в нём не должна доезжать до Telegram живой."""
    text, _ = await _render_report(
        monkeypatch,
        top_referrers=[{'referrer_label': '<b>Вася</b> & Co', 'count': 3}],
    )

    assert '1. &lt;b&gt;Вася&lt;/b&gt; &amp; Co: 3 приглашений' in text
    assert '<b>Вася</b>' not in text


async def test_report_without_donations_when_disabled(monkeypatch):
    text, collector = await _render_report(monkeypatch, donations_enabled=False)

    assert 'Пожертвования' not in text
    # Гейт стоит до обращения к БД
    assert collector.await_count == 0
    # Остальные секции на месте и не склеились
    assert '💰 <b>Финансы</b>' in text
    assert '\n\n🎟️ <b>Поддержка</b>' in text


async def test_donations_section_survives_rich_conversion(monkeypatch):
    text, _ = await _render_report(monkeypatch)

    rich = classic_admin_html_to_rich(text)

    assert '<p>💜 <b>Пожертвования</b><br>• Собрано за период:' in rich
    # Секция — отдельный абзац: «Финансы» и «Поддержка» в неё не затекают
    donations_paragraph = rich.split('<p>💜 <b>Пожертвования</b>')[1].split('</p>')[0]
    assert '💰' not in donations_paragraph
    assert '🎟️' not in donations_paragraph


async def test_report_fits_telegram_limit(monkeypatch):
    text, _ = await _render_report(monkeypatch)

    # Ручная отправка отчёта админу идёт классическим HTML без чанкинга — жёсткий лимит Telegram
    assert len(text) < 4096
    assert len(classic_admin_html_to_rich(text)) < 4096


async def test_report_fits_telegram_limit_with_hostile_labels(monkeypatch):
    # Имя в Telegram — чужой ввод (64+64 символа), и escape раздувает `&` впятеро.
    # Без обрезки 5 рефереров уносили отчёт за 4096 и он не отправлялся вовсе.
    hostile = '&' * 64 + ' ' + '<' * 64
    text, _ = await _render_report(
        monkeypatch,
        top_referrers=[{'referrer_label': hostile, 'count': 10 - index} for index in range(5)],
    )

    assert len(text) < 4096
    assert len(classic_admin_html_to_rich(text)) < 4096
    # Обрезка идёт до экранирования, иначе можно разрубить сущность пополам
    assert '&am;' not in text and '&l;' not in text
    assert '…' in text


async def test_report_shows_other_sources_only_when_present(monkeypatch):
    """Источник помимо кнопки и надбавки не должен молча выпадать из разбивки."""
    text, _ = await _render_report(monkeypatch)
    assert 'Прочие источники' not in text

    text, _ = await _render_report(
        monkeypatch,
        donations=_donation_stats(other_amount=5000, other_count=2),
    )
    assert '• Прочие источники: 50.00 ₽ (2)' in text


async def test_tldr_note_appears_only_with_upsell(monkeypatch):
    """Оговорка про надбавки нужна только тогда, когда надбавки в периоде были."""
    text, _ = await _render_report(
        monkeypatch,
        donations=_donation_stats(upsell_amount=0, upsell_count=0),
    )
    assert '• Пожертвования: <b>3 076.00 ₽</b>\n' in text
    assert 'уже в поступлениях' not in text
