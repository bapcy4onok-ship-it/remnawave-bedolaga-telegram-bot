"""Тесты курсов ЦБ: разбор ответа и пересчёт валютных сумм в копейки."""

from datetime import UTC, datetime, timedelta

import pytest

from app.services.exchange_rate_service import (
    FALLBACK_RATES,
    ExchangeRateService,
    exchange_rate_service,
    parse_cbr_xml,
)


CBR_XML = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="25.08.2026" name="Foreign Currency Market">
<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal>
<Name>Doll</Name><Value>83,2878</Value><VunitRate>83,2878</VunitRate></Valute>
<Valute ID="R01239"><NumCode>978</NumCode><CharCode>EUR</CharCode><Nominal>1</Nominal>
<Name>Euro</Name><Value>97,4384</Value><VunitRate>97,4384</VunitRate></Valute>
<Valute ID="R01375"><NumCode>156</NumCode><CharCode>CNY</CharCode><Nominal>10</Nominal>
<Name>Yuan</Name><Value>115,0</Value><VunitRate>11,5</VunitRate></Valute>
</ValCurs>"""


def test_parse_cbr_xml_extracts_supported_currencies():
    rates, date = parse_cbr_xml(CBR_XML)

    assert date == '25.08.2026'
    assert rates['USD'] == pytest.approx(83.2878)
    assert rates['EUR'] == pytest.approx(97.4384)
    assert rates['RUB'] == 1.0
    # Неподдерживаемые валюты не заводим — их некуда вводить в форме расхода.
    assert 'CNY' not in rates


def test_parse_cbr_xml_applies_nominal():
    """Курс, указанный за номинал (10 единиц), приводится к одной единице."""
    xml = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="25.08.2026">
<Valute ID="R01235"><CharCode>USD</CharCode><Nominal>10</Nominal><Value>830,0</Value></Valute>
<Valute ID="R01239"><CharCode>EUR</CharCode><Nominal>1</Nominal><Value>97,0</Value></Valute>
</ValCurs>"""

    rates, _ = parse_cbr_xml(xml)

    assert rates['USD'] == pytest.approx(83.0)
    assert rates['EUR'] == pytest.approx(97.0)


def test_parse_cbr_xml_on_garbage_returns_none():
    rates, date = parse_cbr_xml('<html>503 Service Unavailable</html>')
    assert rates is None
    assert date is None


@pytest.mark.asyncio
async def test_rub_never_needs_network():
    """Рублёвая сумма не должна ходить за курсом вообще."""
    service = ExchangeRateService()
    assert await service.to_rub_kopeks(150_000, 'RUB') == 150_000
    assert service._fetched_at is None


@pytest.mark.asyncio
async def test_snapshot_rate_wins_over_live(monkeypatch):
    """Переданный курс используется как есть — отчёт за прошлое не переписывается."""
    monkeypatch.setattr(exchange_rate_service, '_rates', {'RUB': 1.0, 'USD': 100.0})
    monkeypatch.setattr(exchange_rate_service, '_fetched_at', datetime.now(UTC))

    assert await exchange_rate_service.to_rub_kopeks(1_000, 'USD') == 100_000
    assert await exchange_rate_service.to_rub_kopeks(1_000, 'USD', rate=75.0) == 75_000


@pytest.mark.asyncio
async def test_falls_back_when_cbr_unreachable(monkeypatch):
    """Недоступный ЦБ не должен ломать отчёт — отдаём зашитые курсы с пометкой."""
    service = ExchangeRateService()

    async def _fail():
        raise RuntimeError('network down')

    monkeypatch.setattr(service, '_fetch_json', _fail)
    monkeypatch.setattr(service, '_fetch_xml', _fail)

    payload = await service.get_rates()

    assert payload['source'] == 'fallback'
    assert payload['rates'] == FALLBACK_RATES


@pytest.mark.asyncio
async def test_stale_real_rates_beat_fallback(monkeypatch):
    """Если ЦБ отвалился после успешного запроса, держим прошлый курс, а не зашитый."""
    service = ExchangeRateService()
    service._rates = {'RUB': 1.0, 'USD': 90.5}
    service._source = 'cbr-json'
    service._fetched_at = datetime.now(UTC) - timedelta(days=2)

    async def _fail():
        raise RuntimeError('network down')

    monkeypatch.setattr(service, '_fetch_json', _fail)
    monkeypatch.setattr(service, '_fetch_xml', _fail)

    payload = await service.get_rates()

    assert payload['rates']['USD'] == 90.5
    assert payload['source'] == 'cbr-json'


@pytest.mark.asyncio
async def test_cached_rates_are_not_refetched(monkeypatch):
    """Свежий кэш не должен дёргать сеть на каждый отчёт."""
    service = ExchangeRateService()
    calls = []

    async def _fetch_json():
        calls.append(1)
        return {'RUB': 1.0, 'USD': 80.0}, '25.08.2026'

    monkeypatch.setattr(service, '_fetch_json', _fetch_json)

    await service.get_rates()
    await service.get_rates()

    assert len(calls) == 1
