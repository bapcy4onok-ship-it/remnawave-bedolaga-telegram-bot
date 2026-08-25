"""Курсы валют ЦБ РФ для пересчёта расходов в рубли.

Нужен странице «Чистая прибыль»: зарубежный хостинг выставляет счета в €/$,
а весь учёт в проекте рублёвый (копейки). Один общий кэш на процесс — курс
меняется раз в сутки, дёргать ЦБ на каждый запрос отчёта незачем.

Источник — ``cbr-xml-daily.ru`` (JSON-зеркало официального XML ЦБ) с откатом
на сам ``cbr.ru``. Если недоступны оба, отдаём зашитые значения и помечаем
ответ ``source='fallback'`` — отчёт должен строиться даже без интернета,
но кабинет обязан показать, что курс приблизительный.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import structlog


logger = structlog.get_logger(__name__)

# Валюты, в которых разрешено заводить расходы. RUB — база, курс всегда 1.
SUPPORTED_CURRENCIES = ('RUB', 'USD', 'EUR')

# Запасные курсы (рублей за единицу) на случай недоступности ЦБ. Порядок величины
# важнее точности: без них отчёт вообще не построился бы.
FALLBACK_RATES: dict[str, float] = {'RUB': 1.0, 'USD': 90.0, 'EUR': 100.0}

_JSON_URL = 'https://www.cbr-xml-daily.ru/daily_json.js'
_XML_URL = 'https://www.cbr.ru/scripts/XML_daily.asp'
_TIMEOUT_SECONDS = 10
_CACHE_TTL = timedelta(hours=6)
# Ответ ЦБ — десятки килобайт; отрезаем всё сверх этого, чтобы разбор не зависел
# от размера того, что придёт.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_VALUTE_RE = re.compile(r'<Valute\b[^>]*>(.*?)</Valute>', re.DOTALL)
_CHAR_CODE_RE = re.compile(r'<CharCode>\s*([A-Za-z]{3})\s*</CharCode>')
_NOMINAL_RE = re.compile(r'<Nominal>\s*([\d\s.,]+?)\s*</Nominal>')
_VALUE_RE = re.compile(r'<Value>\s*([\d\s.,]+?)\s*</Value>')
_DATE_ATTR_RE = re.compile(r'<ValCurs\b[^>]*\bDate="([^"]+)"')


def parse_cbr_xml(text: str) -> tuple[dict[str, float] | None, str | None]:
    """Разбирает ответ ЦБ в ``({'USD': 83.28, ...}, '25.08.2026')``.

    Регулярки, а не ElementTree: XML-парсер на внешних данных — известный класс
    уязвимостей (раздувание сущностей), а формат ответа ЦБ плоский и стабильный,
    полноценный парсер тут не нужен.
    """
    rates = {'RUB': 1.0}
    for block in _VALUTE_RE.findall(text):
        code_match = _CHAR_CODE_RE.search(block)
        value_match = _VALUE_RE.search(block)
        if not code_match or not value_match:
            continue
        code = code_match.group(1).upper()
        if code not in SUPPORTED_CURRENCIES or code == 'RUB':
            continue

        nominal_match = _NOMINAL_RE.search(block)
        try:
            nominal = float((nominal_match.group(1) if nominal_match else '1').replace(',', '.')) or 1
            value = float(value_match.group(1).replace(',', '.'))
        except ValueError:
            continue
        if value > 0:
            rates[code] = value / nominal

    date_match = _DATE_ATTR_RE.search(text)
    return (rates if len(rates) > 1 else None), (date_match.group(1) if date_match else None)


class ExchangeRateService:
    """Кэширующий провайдер курсов «рублей за 1 единицу валюты»."""

    def __init__(self) -> None:
        self._rates: dict[str, float] | None = None
        self._fetched_at: datetime | None = None
        self._source: str = 'fallback'
        self._rate_date: str | None = None
        self._lock = asyncio.Lock()

    async def get_rates(self, *, force: bool = False) -> dict[str, Any]:
        """Курсы + метаданные: ``{'rates': {...}, 'source': ..., 'date': ..., 'fetched_at': ...}``."""
        if not force and self._is_fresh():
            return self._snapshot()

        # Под замком: параллельные запросы отчёта не должны бомбить ЦБ пачкой.
        async with self._lock:
            if not force and self._is_fresh():
                return self._snapshot()

            rates, source, rate_date = await self._fetch()
            if rates:
                self._rates = rates
                self._source = source
                self._rate_date = rate_date
                self._fetched_at = datetime.now(UTC)
            elif self._rates is None:
                # Первая попытка и та неудачная — живём на запасных курсах,
                # но не кэшируем их надолго, чтобы попробовать снова.
                self._rates = dict(FALLBACK_RATES)
                self._source = 'fallback'
                self._rate_date = None
                self._fetched_at = datetime.now(UTC) - _CACHE_TTL + timedelta(minutes=10)
            # Иначе оставляем прошлые курсы: устаревший реальный курс лучше зашитого.

            return self._snapshot()

    async def get_rate(self, currency: str) -> float:
        """Сколько рублей стоит 1 единица ``currency``."""
        code = (currency or 'RUB').upper()
        if code == 'RUB':
            return 1.0
        payload = await self.get_rates()
        return float(payload['rates'].get(code, FALLBACK_RATES.get(code, 1.0)))

    async def to_rub_kopeks(self, amount_minor: int, currency: str, *, rate: float | None = None) -> int:
        """Минорные единицы валюты → копейки.

        ``rate`` позволяет передать зафиксированный курс (снимок на момент оплаты),
        не ходя в сеть.
        """
        code = (currency or 'RUB').upper()
        if code == 'RUB':
            return int(amount_minor)
        effective = rate if rate and rate > 0 else await self.get_rate(code)
        return int(round(amount_minor * effective))

    # ── внутреннее ────────────────────────────────────────────────────────────

    def _is_fresh(self) -> bool:
        return (
            self._rates is not None
            and self._fetched_at is not None
            and datetime.now(UTC) - self._fetched_at < _CACHE_TTL
        )

    def _snapshot(self) -> dict[str, Any]:
        return {
            'rates': dict(self._rates or FALLBACK_RATES),
            'source': self._source,
            'date': self._rate_date,
            'fetched_at': self._fetched_at.isoformat() if self._fetched_at else None,
        }

    async def _fetch(self) -> tuple[dict[str, float] | None, str, str | None]:
        for fetcher, source in ((self._fetch_json, 'cbr-json'), (self._fetch_xml, 'cbr-xml')):
            try:
                rates, rate_date = await fetcher()
            except Exception as error:
                logger.warning('Не удалось получить курсы ЦБ', source=source, error=str(error))
                continue
            if rates:
                return rates, source, rate_date
        return None, 'fallback', None

    async def _fetch_json(self) -> tuple[dict[str, float] | None, str | None]:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(_JSON_URL) as response:
            response.raise_for_status()
            # У зеркала ЦБ Content-Type — application/javascript, поэтому content_type=None.
            payload = await response.json(content_type=None)

        valute = payload.get('Valute') or {}
        rates = {'RUB': 1.0}
        for code in SUPPORTED_CURRENCIES:
            if code == 'RUB':
                continue
            entry = valute.get(code)
            if not entry:
                continue
            nominal = float(entry.get('Nominal') or 1) or 1
            value = float(entry.get('Value') or 0)
            if value > 0:
                rates[code] = value / nominal

        return (rates if len(rates) > 1 else None), payload.get('Date')

    async def _fetch_xml(self) -> tuple[dict[str, float] | None, str | None]:
        """Официальный XML ЦБ — запасной источник, если зеркало недоступно."""
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(_XML_URL) as response:
            response.raise_for_status()
            raw = await response.read()

        # ЦБ отдаёт windows-1251; нужные поля — ASCII, имена валют не читаем.
        return parse_cbr_xml(raw[:_MAX_RESPONSE_BYTES].decode('cp1251', errors='replace'))


exchange_rate_service = ExchangeRateService()
