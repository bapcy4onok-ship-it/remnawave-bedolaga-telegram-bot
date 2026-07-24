"""Донаты «Поддержать проект»: пометка платежа и защита баланса.

Донат — это обычный платёж у любого провайдера, помеченный ``purpose=donation``.
Пометка — единственное, что отличает его от пополнения: по ней вебхук
(``try_fulfill_donation``) не зачисляет деньги на баланс. Тесты фиксируют
именно её, плюс то, что общий рефакторинг не сломал гостевые платежи.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.services.payment_service as payment_service_module
from app.config import settings
from app.services.payment_service import PaymentService


class DummySession:
    async def commit(self) -> None:
        return None


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> PaymentService:
    monkeypatch.setattr(settings, 'HELEKET_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'CRYPTOBOT_ENABLED', True, raising=False)
    instance = PaymentService.__new__(PaymentService)
    instance.bot = None
    instance.heleket_service = object()
    instance.cryptobot_service = object()
    return instance


@pytest.fixture
def patched_metadata(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Перехватываем пометку локального платежа (обычно — UPDATE в БД)."""
    calls: list[dict[str, Any]] = []

    async def fake_patch(db, model_name, local_payment_id, extra):
        calls.append({'model_name': model_name, 'local_payment_id': local_payment_id, 'extra': extra})
        return True

    monkeypatch.setattr(payment_service_module, 'patch_payment_metadata', fake_patch)
    return calls


def _stub_heleket(service: PaymentService, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return {'local_payment_id': 77, 'payment_url': 'https://pay.example/heleket', 'uuid': 'uuid-1'}

    monkeypatch.setattr(service, 'create_heleket_payment', fake_create)
    return calls


@pytest.mark.asyncio
async def test_donation_payment_is_tagged_and_owned_by_donor(service, patched_metadata, monkeypatch):
    """Донат помечается purpose=donation и привязан к пользователю."""
    create_calls = _stub_heleket(service, monkeypatch)

    result = await service.create_donation_payment(
        DummySession(),
        user_id=42,
        amount_kopeks=50000,
        payment_method='heleket',
        description='Пожертвование',
        return_url='https://t.me/bot',
    )

    assert result['payment_url'] == 'https://pay.example/heleket'
    assert create_calls[0]['user_id'] == 42, 'донат должен быть привязан к донору'
    assert patched_metadata[0]['model_name'] == 'heleket'
    assert patched_metadata[0]['extra']['purpose'] == 'donation'


@pytest.mark.asyncio
async def test_donation_without_persisted_marker_returns_no_url(service, monkeypatch):
    """Не удалось записать пометку — ссылку не отдаём.

    Иначе оплата по такой ссылке была бы зачислена на баланс как обычный топап.
    """
    _stub_heleket(service, monkeypatch)

    async def failing_patch(db, model_name, local_payment_id, extra):
        return False

    monkeypatch.setattr(payment_service_module, 'patch_payment_metadata', failing_patch)

    result = await service.create_donation_payment(
        DummySession(),
        user_id=42,
        amount_kopeks=50000,
        payment_method='heleket',
        description='Пожертвование',
        return_url='https://t.me/bot',
    )

    assert result is None


@pytest.mark.asyncio
async def test_guest_payment_marker_survives_failed_patch(service, monkeypatch):
    """Гостевой флоу остаётся мягким к сбою пометки (поведение до рефакторинга)."""
    _stub_heleket(service, monkeypatch)

    async def failing_patch(db, model_name, local_payment_id, extra):
        return False

    monkeypatch.setattr(payment_service_module, 'patch_payment_metadata', failing_patch)

    result = await service.create_guest_payment(
        DummySession(),
        amount_kopeks=50000,
        payment_method='heleket',
        description='Подписка',
        purchase_token='tok-123',
        return_url='https://example.com/return',
    )

    assert result is not None
    assert result['payment_url'] == 'https://pay.example/heleket'


@pytest.mark.asyncio
async def test_guest_payment_still_tags_purchase_token(service, patched_metadata, monkeypatch):
    """Гостевая покупка помечается токеном и создаётся без user_id."""
    create_calls = _stub_heleket(service, monkeypatch)

    await service.create_guest_payment(
        DummySession(),
        amount_kopeks=50000,
        payment_method='heleket',
        description='Подписка',
        purchase_token='tok-123',
        return_url='https://example.com/return',
    )

    assert create_calls[0]['user_id'] is None
    assert patched_metadata[0]['extra'] == {
        'purpose': 'guest_purchase',
        'purchase_token': 'tok-123',
        'source': 'landing',
    }


@pytest.mark.asyncio
async def test_cryptobot_donation_marker_travels_in_payload(service, monkeypatch):
    """У CryptoBot нет metadata_json — пометка и сумма едут в payload."""
    calls: list[dict[str, Any]] = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return {'bot_invoice_url': 'https://t.me/CryptoBot?start=x', 'invoice_id': 5}

    async def fake_rub_to_usd(amount_rubles):
        return 5.0

    monkeypatch.setattr(service, 'create_cryptobot_payment', fake_create)
    monkeypatch.setattr(payment_service_module.currency_converter, 'rub_to_usd', fake_rub_to_usd)

    await service.create_donation_payment(
        DummySession(),
        user_id=42,
        amount_kopeks=50000,
        payment_method='cryptobot',
        description='Пожертвование',
        return_url='https://t.me/bot',
    )

    payload = json.loads(calls[0]['payload'])
    assert payload['purpose'] == 'donation'
    # Процессор CryptoBot берёт рублёвую сумму отсюда, а не пересчитывает из USD.
    assert payload['amount_kopeks'] == 50000
    assert calls[0]['user_id'] == 42


@pytest.mark.asyncio
async def test_stars_donation_uses_balance_donation_payload(service, monkeypatch):
    """Stars: префикс payload — то, по чему хендлер бота узнаёт донат."""
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', True, raising=False)
    captured: dict[str, Any] = {}

    class FakeBot:
        async def create_invoice_link(self, **kwargs):
            captured.update(kwargs)
            return 'https://t.me/invoice/xyz'

    service.bot = FakeBot()

    result = await service.create_donation_payment(
        DummySession(),
        user_id=42,
        amount_kopeks=50000,
        payment_method='telegram_stars',
        description='Пожертвование',
        return_url='https://t.me/bot',
    )

    assert result['payment_url'] == 'https://t.me/invoice/xyz'
    assert captured['payload'].startswith('balance_donation_')
    assert captured['currency'] == 'XTR'
