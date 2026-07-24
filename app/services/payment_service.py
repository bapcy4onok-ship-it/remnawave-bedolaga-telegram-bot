"""Агрегирующий сервис, собирающий все платёжные модули."""

from __future__ import annotations

import json
import time
from importlib import import_module
from typing import Any

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.external.cryptobot import CryptoBotService
from app.external.heleket import HeleketService
from app.external.telegram_stars import TelegramStarsService
from app.services.cloudpayments_service import CloudPaymentsService
from app.services.mulenpay_service import MulenPayService
from app.services.nalogo_service import NaloGoService
from app.services.pal24_service import Pal24Service
from app.services.payment import (
    CryptoBotPaymentMixin,
    HeleketPaymentMixin,
    MulenPayPaymentMixin,
    Pal24PaymentMixin,
    PaymentCommonMixin,
    PlategaPaymentMixin,
    TelegramStarsMixin,
    TributePaymentMixin,
    WataPaymentMixin,
    YooKassaPaymentMixin,
)
from app.services.payment.antilopay import AntilopayPaymentMixin
from app.services.payment.aurapay import AuraPayPaymentMixin
from app.services.payment.cloudpayments import CloudPaymentsPaymentMixin
from app.services.payment.common import patch_payment_metadata
from app.services.payment.donut import DonutPaymentMixin
from app.services.payment.etoplatezhi import EtoplatezhiPaymentMixin
from app.services.payment.freekassa import FreekassaPaymentMixin
from app.services.payment.jupiter import JupiterPaymentMixin
from app.services.payment.kassa_ai import KassaAiPaymentMixin
from app.services.payment.lava import LavaPaymentMixin
from app.services.payment.overpay import OverpayPaymentMixin
from app.services.payment.paypear import PayPearPaymentMixin
from app.services.payment.riopay import RioPayPaymentMixin
from app.services.payment.rollypay import RollyPayPaymentMixin
from app.services.payment.severpay import SeverPayPaymentMixin
from app.services.platega_service import PlategaService
from app.services.wata_service import WataService
from app.services.yookassa_service import YooKassaService
from app.utils.currency_converter import currency_converter


logger = structlog.get_logger(__name__)


# --- Совместимость: экспортируем функции, которые активно мокаются в тестах ---


async def create_yookassa_payment(*args, **kwargs):
    yk_crud = import_module('app.database.crud.yookassa')
    return await yk_crud.create_yookassa_payment(*args, **kwargs)


async def update_yookassa_payment_status(*args, **kwargs):
    yk_crud = import_module('app.database.crud.yookassa')
    return await yk_crud.update_yookassa_payment_status(*args, **kwargs)


async def link_yookassa_payment_to_transaction(*args, **kwargs):
    yk_crud = import_module('app.database.crud.yookassa')
    return await yk_crud.link_yookassa_payment_to_transaction(*args, **kwargs)


async def get_yookassa_payment_by_id(*args, **kwargs):
    yk_crud = import_module('app.database.crud.yookassa')
    return await yk_crud.get_yookassa_payment_by_id(*args, **kwargs)


async def get_yookassa_payment_by_local_id(*args, **kwargs):
    yk_crud = import_module('app.database.crud.yookassa')
    return await yk_crud.get_yookassa_payment_by_local_id(*args, **kwargs)


async def create_transaction(*args, **kwargs):
    transaction_crud = import_module('app.database.crud.transaction')
    return await transaction_crud.create_transaction(*args, **kwargs)


async def get_transaction_by_external_id(*args, **kwargs):
    transaction_crud = import_module('app.database.crud.transaction')
    return await transaction_crud.get_transaction_by_external_id(*args, **kwargs)


async def add_user_balance(*args, **kwargs):
    user_crud = import_module('app.database.crud.user')
    return await user_crud.add_user_balance(*args, **kwargs)


async def get_user_by_id(*args, **kwargs):
    user_crud = import_module('app.database.crud.user')
    return await user_crud.get_user_by_id(*args, **kwargs)


async def get_user_by_telegram_id(*args, **kwargs):
    user_crud = import_module('app.database.crud.user')
    return await user_crud.get_user_by_telegram_id(*args, **kwargs)


async def create_mulenpay_payment(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.create_mulenpay_payment(*args, **kwargs)


async def get_mulenpay_payment_by_uuid(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.get_mulenpay_payment_by_uuid(*args, **kwargs)


async def get_mulenpay_payment_by_mulen_id(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.get_mulenpay_payment_by_mulen_id(*args, **kwargs)


async def get_mulenpay_payment_by_local_id(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.get_mulenpay_payment_by_local_id(*args, **kwargs)


async def update_mulenpay_payment_status(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.update_mulenpay_payment_status(*args, **kwargs)


async def update_mulenpay_payment_metadata(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.update_mulenpay_payment_metadata(*args, **kwargs)


async def link_mulenpay_payment_to_transaction(*args, **kwargs):
    mulenpay_crud = import_module('app.database.crud.mulenpay')
    return await mulenpay_crud.link_mulenpay_payment_to_transaction(*args, **kwargs)


async def create_pal24_payment(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.create_pal24_payment(*args, **kwargs)


async def get_pal24_payment_by_bill_id(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.get_pal24_payment_by_bill_id(*args, **kwargs)


async def get_pal24_payment_by_order_id(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.get_pal24_payment_by_order_id(*args, **kwargs)


async def get_pal24_payment_by_id(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.get_pal24_payment_by_id(*args, **kwargs)


async def update_pal24_payment_status(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.update_pal24_payment_status(*args, **kwargs)


async def link_pal24_payment_to_transaction(*args, **kwargs):
    pal_crud = import_module('app.database.crud.pal24')
    return await pal_crud.link_pal24_payment_to_transaction(*args, **kwargs)


async def create_wata_payment(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.create_wata_payment(*args, **kwargs)


async def get_wata_payment_by_link_id(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.get_wata_payment_by_link_id(*args, **kwargs)


async def get_wata_payment_by_id(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.get_wata_payment_by_id(*args, **kwargs)


# Алиас для совместимости с хендлерами
async def get_wata_payment_by_local_id(*args, **kwargs):
    return await get_wata_payment_by_id(*args, **kwargs)


async def get_wata_payment_by_order_id(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.get_wata_payment_by_order_id(*args, **kwargs)


async def update_wata_payment_status(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.update_wata_payment_status(*args, **kwargs)


async def link_wata_payment_to_transaction(*args, **kwargs):
    wata_crud = import_module('app.database.crud.wata')
    return await wata_crud.link_wata_payment_to_transaction(*args, **kwargs)


async def create_platega_payment(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.create_platega_payment(*args, **kwargs)


async def get_platega_payment_by_id(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.get_platega_payment_by_id(*args, **kwargs)


async def get_platega_payment_by_id_for_update(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.get_platega_payment_by_id_for_update(*args, **kwargs)


async def get_platega_payment_by_transaction_id(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.get_platega_payment_by_transaction_id(*args, **kwargs)


async def get_platega_payment_by_correlation_id(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.get_platega_payment_by_correlation_id(*args, **kwargs)


async def update_platega_payment(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.update_platega_payment(*args, **kwargs)


async def link_platega_payment_to_transaction(*args, **kwargs):
    platega_crud = import_module('app.database.crud.platega')
    return await platega_crud.link_platega_payment_to_transaction(*args, **kwargs)


async def create_cryptobot_payment(*args, **kwargs):
    crypto_crud = import_module('app.database.crud.cryptobot')
    return await crypto_crud.create_cryptobot_payment(*args, **kwargs)


async def get_cryptobot_payment_by_invoice_id(*args, **kwargs):
    crypto_crud = import_module('app.database.crud.cryptobot')
    return await crypto_crud.get_cryptobot_payment_by_invoice_id(*args, **kwargs)


async def update_cryptobot_payment_status(*args, **kwargs):
    crypto_crud = import_module('app.database.crud.cryptobot')
    return await crypto_crud.update_cryptobot_payment_status(*args, **kwargs)


async def link_cryptobot_payment_to_transaction(*args, **kwargs):
    crypto_crud = import_module('app.database.crud.cryptobot')
    return await crypto_crud.link_cryptobot_payment_to_transaction(*args, **kwargs)


async def create_heleket_payment(*args, **kwargs):
    heleket_crud = import_module('app.database.crud.heleket')
    return await heleket_crud.create_heleket_payment(*args, **kwargs)


async def get_heleket_payment_by_uuid(*args, **kwargs):
    heleket_crud = import_module('app.database.crud.heleket')
    return await heleket_crud.get_heleket_payment_by_uuid(*args, **kwargs)


async def get_heleket_payment_by_id(*args, **kwargs):
    heleket_crud = import_module('app.database.crud.heleket')
    return await heleket_crud.get_heleket_payment_by_id(*args, **kwargs)


async def update_heleket_payment(*args, **kwargs):
    heleket_crud = import_module('app.database.crud.heleket')
    return await heleket_crud.update_heleket_payment(*args, **kwargs)


async def link_heleket_payment_to_transaction(*args, **kwargs):
    heleket_crud = import_module('app.database.crud.heleket')
    return await heleket_crud.link_heleket_payment_to_transaction(*args, **kwargs)


async def create_cloudpayments_payment(*args, **kwargs):
    cloudpayments_crud = import_module('app.database.crud.cloudpayments')
    return await cloudpayments_crud.create_cloudpayments_payment(*args, **kwargs)


async def get_cloudpayments_payment_by_invoice_id(*args, **kwargs):
    cloudpayments_crud = import_module('app.database.crud.cloudpayments')
    return await cloudpayments_crud.get_cloudpayments_payment_by_invoice_id(*args, **kwargs)


async def get_cloudpayments_payment_by_id(*args, **kwargs):
    cloudpayments_crud = import_module('app.database.crud.cloudpayments')
    return await cloudpayments_crud.get_cloudpayments_payment_by_id(*args, **kwargs)


async def update_cloudpayments_payment(*args, **kwargs):
    cloudpayments_crud = import_module('app.database.crud.cloudpayments')
    return await cloudpayments_crud.update_cloudpayments_payment(*args, **kwargs)


async def create_severpay_payment(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.create_severpay_payment(*args, **kwargs)


async def get_severpay_payment_by_order_id(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.get_severpay_payment_by_order_id(*args, **kwargs)


async def get_severpay_payment_by_severpay_id(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.get_severpay_payment_by_severpay_id(*args, **kwargs)


async def get_severpay_payment_by_id(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.get_severpay_payment_by_id(*args, **kwargs)


async def get_severpay_payment_by_id_for_update(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.get_severpay_payment_by_id_for_update(*args, **kwargs)


async def update_severpay_payment_status(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.update_severpay_payment_status(*args, **kwargs)


async def link_severpay_payment_to_transaction(*args, **kwargs):
    severpay_crud = import_module('app.database.crud.severpay')
    return await severpay_crud.link_severpay_payment_to_transaction(*args, **kwargs)


async def create_paypear_payment(*args, **kwargs):
    paypear_crud = import_module('app.database.crud.paypear')
    return await paypear_crud.create_paypear_payment(*args, **kwargs)


async def get_paypear_payment_by_order_id(*args, **kwargs):
    paypear_crud = import_module('app.database.crud.paypear')
    return await paypear_crud.get_paypear_payment_by_order_id(*args, **kwargs)


async def get_paypear_payment_by_paypear_id(*args, **kwargs):
    paypear_crud = import_module('app.database.crud.paypear')
    return await paypear_crud.get_paypear_payment_by_paypear_id(*args, **kwargs)


async def get_paypear_payment_by_id(*args, **kwargs):
    paypear_crud = import_module('app.database.crud.paypear')
    return await paypear_crud.get_paypear_payment_by_id(*args, **kwargs)


async def get_paypear_payment_by_id_for_update(*args, **kwargs):
    paypear_crud = import_module('app.database.crud.paypear')
    return await paypear_crud.get_paypear_payment_by_id_for_update(*args, **kwargs)


async def update_paypear_payment_status(*args, **kwargs):
    paypear_crud = import_module('app.database.crud.paypear')
    return await paypear_crud.update_paypear_payment_status(*args, **kwargs)


async def link_paypear_payment_to_transaction(*args, **kwargs):
    paypear_crud = import_module('app.database.crud.paypear')
    return await paypear_crud.link_paypear_payment_to_transaction(*args, **kwargs)


# --- RollyPay CRUD wrappers ---


async def create_rollypay_payment(*args, **kwargs):
    rollypay_crud = import_module('app.database.crud.rollypay')
    return await rollypay_crud.create_rollypay_payment(*args, **kwargs)


async def get_rollypay_payment_by_order_id(*args, **kwargs):
    rollypay_crud = import_module('app.database.crud.rollypay')
    return await rollypay_crud.get_rollypay_payment_by_order_id(*args, **kwargs)


async def get_rollypay_payment_by_rollypay_id(*args, **kwargs):
    rollypay_crud = import_module('app.database.crud.rollypay')
    return await rollypay_crud.get_rollypay_payment_by_rollypay_id(*args, **kwargs)


async def get_rollypay_payment_by_id(*args, **kwargs):
    rollypay_crud = import_module('app.database.crud.rollypay')
    return await rollypay_crud.get_rollypay_payment_by_id(*args, **kwargs)


async def get_rollypay_payment_by_id_for_update(*args, **kwargs):
    rollypay_crud = import_module('app.database.crud.rollypay')
    return await rollypay_crud.get_rollypay_payment_by_id_for_update(*args, **kwargs)


async def update_rollypay_payment_status(*args, **kwargs):
    rollypay_crud = import_module('app.database.crud.rollypay')
    return await rollypay_crud.update_rollypay_payment_status(*args, **kwargs)


async def link_rollypay_payment_to_transaction(*args, **kwargs):
    rollypay_crud = import_module('app.database.crud.rollypay')
    return await rollypay_crud.link_rollypay_payment_to_transaction(*args, **kwargs)


# --- Overpay CRUD wrappers ---


async def create_overpay_payment(*args, **kwargs):
    overpay_crud = import_module('app.database.crud.overpay')
    return await overpay_crud.create_overpay_payment(*args, **kwargs)


async def get_overpay_payment_by_order_id(*args, **kwargs):
    overpay_crud = import_module('app.database.crud.overpay')
    return await overpay_crud.get_overpay_payment_by_order_id(*args, **kwargs)


async def get_overpay_payment_by_overpay_id(*args, **kwargs):
    overpay_crud = import_module('app.database.crud.overpay')
    return await overpay_crud.get_overpay_payment_by_overpay_id(*args, **kwargs)


async def get_overpay_payment_by_id(*args, **kwargs):
    overpay_crud = import_module('app.database.crud.overpay')
    return await overpay_crud.get_overpay_payment_by_id(*args, **kwargs)


async def get_overpay_payment_by_id_for_update(*args, **kwargs):
    overpay_crud = import_module('app.database.crud.overpay')
    return await overpay_crud.get_overpay_payment_by_id_for_update(*args, **kwargs)


async def update_overpay_payment_status(*args, **kwargs):
    overpay_crud = import_module('app.database.crud.overpay')
    return await overpay_crud.update_overpay_payment_status(*args, **kwargs)


async def link_overpay_payment_to_transaction(*args, **kwargs):
    overpay_crud = import_module('app.database.crud.overpay')
    return await overpay_crud.link_overpay_payment_to_transaction(*args, **kwargs)


async def create_aurapay_payment(*args, **kwargs):
    aurapay_crud = import_module('app.database.crud.aurapay')
    return await aurapay_crud.create_aurapay_payment(*args, **kwargs)


async def get_aurapay_payment_by_order_id(*args, **kwargs):
    aurapay_crud = import_module('app.database.crud.aurapay')
    return await aurapay_crud.get_aurapay_payment_by_order_id(*args, **kwargs)


async def get_aurapay_payment_by_invoice_id(*args, **kwargs):
    aurapay_crud = import_module('app.database.crud.aurapay')
    return await aurapay_crud.get_aurapay_payment_by_invoice_id(*args, **kwargs)


async def get_aurapay_payment_by_id(*args, **kwargs):
    aurapay_crud = import_module('app.database.crud.aurapay')
    return await aurapay_crud.get_aurapay_payment_by_id(*args, **kwargs)


async def get_aurapay_payment_by_id_for_update(*args, **kwargs):
    aurapay_crud = import_module('app.database.crud.aurapay')
    return await aurapay_crud.get_aurapay_payment_by_id_for_update(*args, **kwargs)


async def update_aurapay_payment_status(*args, **kwargs):
    aurapay_crud = import_module('app.database.crud.aurapay')
    return await aurapay_crud.update_aurapay_payment_status(*args, **kwargs)


async def link_aurapay_payment_to_transaction(*args, **kwargs):
    aurapay_crud = import_module('app.database.crud.aurapay')
    return await aurapay_crud.link_aurapay_payment_to_transaction(*args, **kwargs)


async def create_etoplatezhi_payment(*args, **kwargs):
    etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
    return await etoplatezhi_crud.create_etoplatezhi_payment(*args, **kwargs)


async def get_etoplatezhi_payment_by_order_id(*args, **kwargs):
    etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
    return await etoplatezhi_crud.get_etoplatezhi_payment_by_order_id(*args, **kwargs)


async def get_etoplatezhi_payment_by_invoice_id(*args, **kwargs):
    etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
    return await etoplatezhi_crud.get_etoplatezhi_payment_by_invoice_id(*args, **kwargs)


async def get_etoplatezhi_payment_by_id(*args, **kwargs):
    etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
    return await etoplatezhi_crud.get_etoplatezhi_payment_by_id(*args, **kwargs)


async def get_etoplatezhi_payment_by_id_for_update(*args, **kwargs):
    etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
    return await etoplatezhi_crud.get_etoplatezhi_payment_by_id_for_update(*args, **kwargs)


async def update_etoplatezhi_payment_status(*args, **kwargs):
    etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
    return await etoplatezhi_crud.update_etoplatezhi_payment_status(*args, **kwargs)


async def link_etoplatezhi_payment_to_transaction(*args, **kwargs):
    etoplatezhi_crud = import_module('app.database.crud.etoplatezhi')
    return await etoplatezhi_crud.link_etoplatezhi_payment_to_transaction(*args, **kwargs)


async def create_antilopay_payment(*args, **kwargs):
    antilopay_crud = import_module('app.database.crud.antilopay')
    return await antilopay_crud.create_antilopay_payment(*args, **kwargs)


async def get_antilopay_payment_by_order_id(*args, **kwargs):
    antilopay_crud = import_module('app.database.crud.antilopay')
    return await antilopay_crud.get_antilopay_payment_by_order_id(*args, **kwargs)


async def get_antilopay_payment_by_invoice_id(*args, **kwargs):
    antilopay_crud = import_module('app.database.crud.antilopay')
    return await antilopay_crud.get_antilopay_payment_by_invoice_id(*args, **kwargs)


async def get_antilopay_payment_by_id(*args, **kwargs):
    antilopay_crud = import_module('app.database.crud.antilopay')
    return await antilopay_crud.get_antilopay_payment_by_id(*args, **kwargs)


async def get_antilopay_payment_by_id_for_update(*args, **kwargs):
    antilopay_crud = import_module('app.database.crud.antilopay')
    return await antilopay_crud.get_antilopay_payment_by_id_for_update(*args, **kwargs)


async def update_antilopay_payment_status(*args, **kwargs):
    antilopay_crud = import_module('app.database.crud.antilopay')
    return await antilopay_crud.update_antilopay_payment_status(*args, **kwargs)


async def link_antilopay_payment_to_transaction(*args, **kwargs):
    antilopay_crud = import_module('app.database.crud.antilopay')
    return await antilopay_crud.link_antilopay_payment_to_transaction(*args, **kwargs)


async def create_jupiter_payment(*args, **kwargs):
    jupiter_crud = import_module('app.database.crud.jupiter')
    return await jupiter_crud.create_jupiter_payment(*args, **kwargs)


async def get_jupiter_payment_by_order_id(*args, **kwargs):
    jupiter_crud = import_module('app.database.crud.jupiter')
    return await jupiter_crud.get_jupiter_payment_by_order_id(*args, **kwargs)


async def get_jupiter_payment_by_invoice_id(*args, **kwargs):
    jupiter_crud = import_module('app.database.crud.jupiter')
    return await jupiter_crud.get_jupiter_payment_by_invoice_id(*args, **kwargs)


async def get_jupiter_payment_by_id(*args, **kwargs):
    jupiter_crud = import_module('app.database.crud.jupiter')
    return await jupiter_crud.get_jupiter_payment_by_id(*args, **kwargs)


async def get_jupiter_payment_by_id_for_update(*args, **kwargs):
    jupiter_crud = import_module('app.database.crud.jupiter')
    return await jupiter_crud.get_jupiter_payment_by_id_for_update(*args, **kwargs)


async def update_jupiter_payment_status(*args, **kwargs):
    jupiter_crud = import_module('app.database.crud.jupiter')
    return await jupiter_crud.update_jupiter_payment_status(*args, **kwargs)


async def link_jupiter_payment_to_transaction(*args, **kwargs):
    jupiter_crud = import_module('app.database.crud.jupiter')
    return await jupiter_crud.link_jupiter_payment_to_transaction(*args, **kwargs)


async def create_donut_payment(*args, **kwargs):
    donut_crud = import_module('app.database.crud.donut')
    return await donut_crud.create_donut_payment(*args, **kwargs)


async def get_donut_payment_by_order_id(*args, **kwargs):
    donut_crud = import_module('app.database.crud.donut')
    return await donut_crud.get_donut_payment_by_order_id(*args, **kwargs)


async def get_donut_payment_by_invoice_id(*args, **kwargs):
    donut_crud = import_module('app.database.crud.donut')
    return await donut_crud.get_donut_payment_by_invoice_id(*args, **kwargs)


async def get_donut_payment_by_id(*args, **kwargs):
    donut_crud = import_module('app.database.crud.donut')
    return await donut_crud.get_donut_payment_by_id(*args, **kwargs)


async def get_donut_payment_by_id_for_update(*args, **kwargs):
    donut_crud = import_module('app.database.crud.donut')
    return await donut_crud.get_donut_payment_by_id_for_update(*args, **kwargs)


async def update_donut_payment_status(*args, **kwargs):
    donut_crud = import_module('app.database.crud.donut')
    return await donut_crud.update_donut_payment_status(*args, **kwargs)


async def link_donut_payment_to_transaction(*args, **kwargs):
    donut_crud = import_module('app.database.crud.donut')
    return await donut_crud.link_donut_payment_to_transaction(*args, **kwargs)


async def create_lava_payment(*args, **kwargs):
    lava_crud = import_module('app.database.crud.lava')
    return await lava_crud.create_lava_payment(*args, **kwargs)


async def get_lava_payment_by_order_id(*args, **kwargs):
    lava_crud = import_module('app.database.crud.lava')
    return await lava_crud.get_lava_payment_by_order_id(*args, **kwargs)


async def get_lava_payment_by_invoice_id(*args, **kwargs):
    lava_crud = import_module('app.database.crud.lava')
    return await lava_crud.get_lava_payment_by_invoice_id(*args, **kwargs)


async def get_lava_payment_by_id(*args, **kwargs):
    lava_crud = import_module('app.database.crud.lava')
    return await lava_crud.get_lava_payment_by_id(*args, **kwargs)


async def get_lava_payment_by_id_for_update(*args, **kwargs):
    lava_crud = import_module('app.database.crud.lava')
    return await lava_crud.get_lava_payment_by_id_for_update(*args, **kwargs)


async def update_lava_payment_status(*args, **kwargs):
    lava_crud = import_module('app.database.crud.lava')
    return await lava_crud.update_lava_payment_status(*args, **kwargs)


async def link_lava_payment_to_transaction(*args, **kwargs):
    lava_crud = import_module('app.database.crud.lava')
    return await lava_crud.link_lava_payment_to_transaction(*args, **kwargs)


def _split_guest_payment_method(payment_method: str) -> tuple[str, str | None]:
    """Разбивает метод гостевого платежа на базовый шлюз + под-опцию.

    Кабинет кодирует выбор СБП/карты/крипто суффиксом метода ("rollypay_sbp"),
    тогда как в обычном (баланс) флоу метод и опция — отдельные поля. Сплитим
    склейку обратно, чтобы ветка шлюза матчилась по базовому ключу, а под-метод
    не терялся. Базовые ключи берём из канонического enum PaymentMethod (НЕ
    хардкодим список — новый шлюз подхватывается автоматически).

    ('rollypay_sbp') -> ('rollypay', 'sbp');  ('rollypay') -> ('rollypay', None).
    """
    from app.database.models import PaymentMethod

    for member in PaymentMethod:
        base = member.value
        if payment_method == base:
            return base, None
        if payment_method.startswith(f'{base}_'):
            return base, payment_method[len(base) + 1 :] or None
    return payment_method, None


class PaymentService(
    PaymentCommonMixin,
    TelegramStarsMixin,
    YooKassaPaymentMixin,
    TributePaymentMixin,
    CryptoBotPaymentMixin,
    HeleketPaymentMixin,
    MulenPayPaymentMixin,
    Pal24PaymentMixin,
    PlategaPaymentMixin,
    WataPaymentMixin,
    CloudPaymentsPaymentMixin,
    FreekassaPaymentMixin,
    KassaAiPaymentMixin,
    RioPayPaymentMixin,
    SeverPayPaymentMixin,
    PayPearPaymentMixin,
    RollyPayPaymentMixin,
    OverpayPaymentMixin,
    AuraPayPaymentMixin,
    EtoplatezhiPaymentMixin,
    AntilopayPaymentMixin,
    JupiterPaymentMixin,
    DonutPaymentMixin,
    LavaPaymentMixin,
):
    """Основной интерфейс платежей, делегирующий работу специализированным mixin-ам."""

    def __init__(self, bot: Bot | None = None) -> None:
        # Бот нужен для отправки уведомлений и создания звёздных инвойсов.
        self.bot = bot
        # Ниже инициализируем службы-обёртки только если соответствующий провайдер включён.
        self.yookassa_service = YooKassaService() if settings.is_yookassa_enabled() else None
        self.stars_service = TelegramStarsService(bot) if bot else None
        self.cryptobot_service = CryptoBotService() if settings.is_cryptobot_enabled() else None
        self.heleket_service = HeleketService() if settings.is_heleket_enabled() else None
        self.mulenpay_service = MulenPayService() if settings.is_mulenpay_enabled() else None
        self.pal24_service = Pal24Service() if settings.is_pal24_enabled() else None
        self.platega_service = PlategaService() if settings.is_platega_enabled() else None
        self.wata_service = WataService() if settings.is_wata_enabled() else None
        self.cloudpayments_service = CloudPaymentsService() if settings.is_cloudpayments_enabled() else None
        self.nalogo_service = NaloGoService() if settings.is_nalogo_enabled() else None

        mulenpay_name = settings.get_mulenpay_display_name()
        logger.debug(
            'PaymentService инициализирован',
            yookassa_service=bool(self.yookassa_service),
            stars_service=bool(self.stars_service),
            cryptobot_service=bool(self.cryptobot_service),
            heleket_service=bool(self.heleket_service),
            mulenpay_name=mulenpay_name,
            mulenpay_service=bool(self.mulenpay_service),
            pal24_service=bool(self.pal24_service),
            platega_service=bool(self.platega_service),
            wata_service=bool(self.wata_service),
            cloudpayments_service=bool(self.cloudpayments_service),
        )

    # ------------------------------------------------------------------
    # Guest (landing page) payments
    # ------------------------------------------------------------------
    # Supported providers for guest payments (to be extended per-provider):
    #   - yookassa (card, sbp)
    #   - cryptobot
    #   - heleket
    #   - mulenpay
    #   - pal24
    #   - platega
    #   - wata
    #   - cloudpayments
    #   - freekassa
    #   - kassa_ai
    #   - telegram_stars
    #   - tribute
    # Each provider returns a different result dict; the caller must
    # extract the payment URL from the provider-specific key.
    # ------------------------------------------------------------------

    async def _create_tagged_payment(
        self,
        db: AsyncSession,
        *,
        amount_kopeks: int,
        payment_method: str,
        description: str,
        return_url: str,
        metadata: dict[str, Any],
        purpose: str,
        user_id: int | None = None,
        stars_title: str,
        stars_label: str,
        stars_payload: str,
        crypto_asset: str = 'USDT',
        strict_metadata: bool = False,
    ) -> dict[str, Any] | None:
        """Create a payment at any provider, tagged with a purpose marker.

        The marker (``metadata``) is stored in the provider's ``metadata_json``
        (or, for CryptoBot/Stars, in the payload) so that the webhook processor
        recognises the payment later instead of crediting the balance as a plain
        top-up. Backs both the guest-purchase flow (``create_guest_payment``) and
        donations (``create_donation_payment``).

        Args:
            user_id: owner of the local payment row (``None`` for guests).
            strict_metadata: when True, a failed metadata patch aborts with
                ``None`` instead of returning a payment URL whose marker was not
                persisted — for donations that would silently credit the balance.

        Returns a provider-specific dict with at least ``payment_url`` on
        success, or ``None`` when the requested provider is unavailable or the
        creation call fails.
        """

        async def _patch_metadata(local_payment_id: int, model_name: str) -> bool:
            """Merge the purpose marker into the local payment's metadata_json."""
            if await patch_payment_metadata(db, model_name, local_payment_id, metadata):
                return True
            logger.warning(
                'Failed to tag payment metadata',
                model_name=model_name,
                local_payment_id=local_payment_id,
                purpose=purpose,
            )
            return not strict_metadata

        # Кабинет шлёт выбор под-метода склейкой ("rollypay_sbp") — разбиваем на
        # базовый шлюз + опцию. Шлюзы, что сами разбирают свой суффикс (yookassa,
        # pal24, freekassa, kassa_ai), матчатся по исходному payment_method ниже;
        # одно-эндпоинтные (rollypay, overpay, lava...) — по _base, с пробросом _option.
        _base, _option = _split_guest_payment_method(payment_method)

        # --- YooKassa (card / sbp) -------------------------------------------
        if payment_method in ('yookassa', 'yookassa_card', 'yookassa_sbp'):
            if self.yookassa_service is None:
                logger.warning('YooKassa is not enabled, cannot create payment')
                return None

            option = 'sbp' if payment_method == 'yookassa_sbp' else 'card'

            if option == 'sbp':
                result = await self.create_yookassa_sbp_payment(
                    db=db,
                    user_id=user_id,
                    amount_kopeks=amount_kopeks,
                    description=description,
                    metadata=metadata,
                    return_url=return_url,
                )
            else:
                result = await self.create_yookassa_payment(
                    db=db,
                    user_id=user_id,
                    amount_kopeks=amount_kopeks,
                    description=description,
                    metadata=metadata,
                    return_url=return_url,
                )

            if result:
                return {
                    'payment_url': result.get('confirmation_url'),
                    'payment_id': result.get('yookassa_payment_id'),
                    'provider': 'yookassa',
                }
            return None

        # --- CryptoBot --------------------------------------------------------
        if payment_method == 'cryptobot':
            if self.cryptobot_service is None:
                logger.warning('CryptoBot is not enabled, cannot create payment')
                return None

            amount_rubles = amount_kopeks / 100
            try:
                amount_usd = await currency_converter.rub_to_usd(amount_rubles)
            except Exception as conv_error:
                logger.error('Currency conversion failed for CryptoBot payment', error=conv_error, purpose=purpose)
                return None

            # CryptoBot has no metadata_json column — the marker travels in the
            # payload string. amount_kopeks rides along so the processor records
            # the exact RUB amount instead of converting USD back.
            payload_str = json.dumps({**metadata, 'amount_kopeks': amount_kopeks}, ensure_ascii=False)

            result = await self.create_cryptobot_payment(
                db=db,
                user_id=user_id,
                amount_usd=amount_usd,
                asset=crypto_asset,
                description=description,
                payload=payload_str,
            )
            if result:
                # CryptoBot stores the marker in the payload field (no metadata_json column)
                payment_url = result.get('bot_invoice_url') or result.get('mini_app_invoice_url')
                return {
                    'payment_url': payment_url,
                    'payment_id': result.get('invoice_id'),
                    'provider': 'cryptobot',
                }
            return None

        # --- Heleket ----------------------------------------------------------
        if payment_method == 'heleket':
            if self.heleket_service is None:
                logger.warning('Heleket is not enabled, cannot create payment')
                return None

            result = await self.create_heleket_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'heleket'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('uuid'),
                    'provider': 'heleket',
                }
            return None

        # --- MulenPay ---------------------------------------------------------
        if payment_method == 'mulenpay':
            if self.mulenpay_service is None:
                logger.warning('MulenPay is not enabled, cannot create payment')
                return None

            result = await self.create_mulenpay_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'mulenpay'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('uuid'),
                    'provider': 'mulenpay',
                }
            return None

        # --- Pal24 (PayPalych) ------------------------------------------------
        if payment_method in ('pal24', 'pal24_sbp', 'pal24_card'):
            if self.pal24_service is None:
                logger.warning('Pal24 is not enabled, cannot create payment')
                return None

            pal24_method = (
                'sbp' if payment_method == 'pal24_sbp' else ('card' if payment_method == 'pal24_card' else None)
            )

            result = await self.create_pal24_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                language=settings.DEFAULT_LANGUAGE,
                payment_method=pal24_method,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'pal24'):
                    return None
                return {
                    'payment_url': result.get('payment_url') or result.get('primary_url'),
                    'payment_id': result.get('bill_id'),
                    'provider': 'pal24',
                }
            return None

        # --- Platega ----------------------------------------------------------
        if payment_method.startswith('platega'):
            if self.platega_service is None:
                logger.warning('Platega is not enabled, cannot create payment')
                return None

            # Extract method code: "platega_2" -> 2, "platega" -> first active method
            method_code: int | None = None
            if '_' in payment_method:
                suffix = payment_method.split('_', 1)[1]
                try:
                    method_code = int(suffix)
                except ValueError:
                    pass

            if method_code is None:
                active_methods = settings.get_platega_active_methods()
                method_code = active_methods[0] if active_methods else 2

            result = await self.create_platega_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                language=settings.DEFAULT_LANGUAGE,
                payment_method_code=method_code,
                return_url=return_url,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'platega'):
                    return None
                return {
                    'payment_url': result.get('redirect_url'),
                    'payment_id': result.get('correlation_id'),
                    'provider': 'platega',
                }
            return None

        # --- WATA -------------------------------------------------------------
        if payment_method == 'wata':
            if self.wata_service is None:
                logger.warning('WATA is not enabled, cannot create payment')
                return None

            result = await self.create_wata_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'wata'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('payment_link_id'),
                    'provider': 'wata',
                }
            return None

        # --- CloudPayments ----------------------------------------------------
        if payment_method == 'cloudpayments':
            if self.cloudpayments_service is None:
                logger.warning('CloudPayments is not enabled, cannot create payment')
                return None

            result = await self.create_cloudpayments_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
            )
            if result:
                if not await _patch_metadata(result['payment_id'], 'cloudpayments'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('invoice_id'),
                    'provider': 'cloudpayments',
                }
            return None

        # --- Freekassa --------------------------------------------------------
        if payment_method in ('freekassa', 'freekassa_sbp', 'freekassa_card'):
            if not settings.is_freekassa_enabled():
                logger.warning('Freekassa is not enabled, cannot create payment')
                return None

            result = await self.create_freekassa_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                payment_method=payment_method,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'freekassa'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('order_id'),
                    'provider': 'freekassa',
                }
            return None

        # --- KassaAI ----------------------------------------------------------
        if payment_method in ('kassa_ai', 'kassa_ai_sbp', 'kassa_ai_card', 'kassa_ai_sberpay'):
            if not settings.is_kassa_ai_enabled():
                logger.warning('KassaAI is not enabled, cannot create payment')
                return None

            from app.services.kassa_ai_service import KASSA_AI_SUB_METHODS

            sub = KASSA_AI_SUB_METHODS.get(payment_method)
            ps_id = sub['payment_system_id'] if sub else None

            result = await self.create_kassa_ai_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                payment_system_id=ps_id,
                return_url=return_url,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'kassa_ai'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('order_id'),
                    'provider': payment_method,
                }
            return None

        # --- RioPay -----------------------------------------------------------
        if _base == 'riopay':
            if not settings.is_riopay_enabled():
                logger.warning('RioPay is not enabled, cannot create payment')
                return None

            result = await self.create_riopay_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                success_url=return_url,
                fail_url=return_url,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'riopay'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('riopay_order_id') or result.get('order_id'),
                    'provider': 'riopay',
                }
            return None

        # --- SeverPay ---------------------------------------------------------
        if _base == 'severpay':
            if not settings.is_severpay_enabled():
                logger.warning('SeverPay is not enabled, cannot create payment')
                return None

            result = await self.create_severpay_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'severpay'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('severpay_id') or result.get('order_id'),
                    'provider': 'severpay',
                }
            return None

        # --- PayPear ----------------------------------------------------------
        if _base == 'paypear':
            if not settings.is_paypear_enabled():
                logger.warning('PayPear is not enabled, cannot create payment')
                return None

            result = await self.create_paypear_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'paypear'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('paypear_id') or result.get('order_id'),
                    'provider': 'paypear',
                }
            return None

        # --- RollyPay ---------------------------------------------------------
        if _base == 'rollypay':
            if not settings.is_rollypay_enabled():
                logger.warning('RollyPay is not enabled, cannot create payment')
                return None

            result = await self.create_rollypay_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
                payment_method_type=_option,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'rollypay'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('rollypay_payment_id') or result.get('order_id'),
                    'provider': 'rollypay',
                }
            return None

        # --- Overpay ----------------------------------------------------------
        if _base == 'overpay':
            if not settings.is_overpay_enabled():
                logger.warning('Overpay is not enabled, cannot create payment')
                return None

            result = await self.create_overpay_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
                option=_option,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'overpay'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('overpay_payment_id') or result.get('order_id'),
                    'provider': 'overpay',
                }
            return None

        # --- AuraPay ----------------------------------------------------------
        if _base == 'aurapay':
            if not settings.is_aurapay_enabled():
                logger.warning('AuraPay is not enabled, cannot create payment')
                return None

            result = await self.create_aurapay_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
                payment_method_type=_option,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'aurapay'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('aurapay_invoice_id') or result.get('order_id'),
                    'provider': 'aurapay',
                }
            return None

        # --- Etoplatezhi ------------------------------------------------------
        if _base == 'etoplatezhi':
            if not settings.is_etoplatezhi_enabled():
                logger.warning('Etoplatezhi is not enabled, cannot create payment')
                return None

            result = await self.create_etoplatezhi_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
                payment_method_type=_option,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'etoplatezhi'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('order_id'),
                    'provider': 'etoplatezhi',
                }
            return None

        # --- Antilopay --------------------------------------------------------
        if _base == 'antilopay':
            if not settings.is_antilopay_enabled():
                logger.warning('Antilopay is not enabled, cannot create payment')
                return None

            result = await self.create_antilopay_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
                payment_method_type=_option,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'antilopay'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('order_id'),
                    'provider': 'antilopay',
                }
            return None

        # --- Jupiter ----------------------------------------------------------
        if _base == 'jupiter':
            if not settings.is_jupiter_enabled():
                logger.warning('Jupiter is not enabled, cannot create payment')
                return None

            result = await self.create_jupiter_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
                payment_method_type=_option,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'jupiter'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('order_id'),
                    'provider': 'jupiter',
                }
            return None

        # --- Donut ------------------------------------------------------------
        if _base == 'donut':
            if not settings.is_donut_enabled():
                logger.warning('Donut is not enabled, cannot create payment')
                return None

            result = await self.create_donut_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
                payment_method_type=_option,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'donut'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('order_id'),
                    'provider': 'donut',
                }
            return None

        # --- Lava -------------------------------------------------------------
        if _base == 'lava':
            if not settings.is_lava_enabled():
                logger.warning('Lava is not enabled, cannot create payment')
                return None

            result = await self.create_lava_payment(
                db=db,
                user_id=user_id,
                amount_kopeks=amount_kopeks,
                description=description,
                return_url=return_url,
                payment_method_type=_option,
            )
            if result:
                if not await _patch_metadata(result['local_payment_id'], 'lava'):
                    return None
                return {
                    'payment_url': result.get('payment_url'),
                    'payment_id': result.get('order_id'),
                    'provider': 'lava',
                }
            return None

        # --- Telegram Stars ---------------------------------------------------
        if payment_method == 'telegram_stars':
            if not settings.TELEGRAM_STARS_ENABLED:
                logger.warning('Telegram Stars is not enabled, cannot create payment')
                return None

            if self.bot is None:
                logger.warning('Bot instance required for Stars guest payment')
                return None

            from aiogram.types import LabeledPrice

            rate = settings.get_stars_rate()
            if rate <= 0:
                logger.error('TELEGRAM_STARS_RATE_RUB is not positive, cannot create Stars invoice')
                return None

            amount_rubles = amount_kopeks / 100
            stars_amount = max(1, round(amount_rubles / rate))

            try:
                invoice_url = await self.bot.create_invoice_link(
                    title=stars_title,
                    description=f'{description} ({stars_amount} ⭐)',
                    payload=stars_payload,
                    provider_token='',
                    currency='XTR',
                    prices=[LabeledPrice(label=stars_label, amount=stars_amount)],
                )

                logger.info(
                    'Created Stars invoice',
                    stars_amount=stars_amount,
                    purpose=purpose,
                )
                return {
                    'payment_url': invoice_url,
                    'payment_id': f'stars_{stars_payload[-24:]}',
                    'provider': 'telegram_stars',
                }

            except Exception as stars_error:
                logger.error('Error creating Stars invoice', error=stars_error, purpose=purpose)
                return None

        # --- Unsupported provider ---------------------------------------------
        logger.warning(
            'Payment requested for unsupported provider',
            payment_method=payment_method,
            purpose=purpose,
        )
        return None

    async def create_guest_payment(
        self,
        db: AsyncSession,
        *,
        amount_kopeks: int,
        payment_method: str,
        description: str,
        purchase_token: str,
        return_url: str,
    ) -> dict[str, Any] | None:
        """Create a payment for a guest (unauthenticated) landing-page purchase.

        Stores ``purchase_token`` in payment metadata so that webhook handlers
        can match the completed payment back to the corresponding
        :class:`GuestPurchase` record.
        """
        return await self._create_tagged_payment(
            db,
            amount_kopeks=amount_kopeks,
            payment_method=payment_method,
            description=description,
            return_url=return_url,
            metadata={
                'purpose': 'guest_purchase',
                'purchase_token': purchase_token,
                'source': 'landing',
            },
            purpose='guest_purchase',
            user_id=None,
            stars_title='Подарочная подписка VPN',
            stars_label='Подарочная подписка',
            stars_payload=f'guest_purchase_{purchase_token}',
        )

    async def create_donation_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        amount_kopeks: int,
        payment_method: str,
        description: str,
        return_url: str,
    ) -> dict[str, Any] | None:
        """Create a «Support the project» donation payment.

        Same providers as a normal top-up, but tagged ``purpose=donation`` so the
        webhook processor records it as a donation instead of crediting the
        balance (see ``app/services/payment/common.py::try_fulfill_donation``).
        Telegram Stars travels through the ``balance_donation_`` payload prefix
        handled in ``app/handlers/stars_payments.py``.

        ``strict_metadata=True``: if the marker cannot be persisted we return
        ``None`` rather than a payment URL that would land on the balance.
        """
        return await self._create_tagged_payment(
            db,
            amount_kopeks=amount_kopeks,
            payment_method=payment_method,
            description=description,
            return_url=return_url,
            # Только строки: у YooKassa metadata уходит в API как есть.
            metadata={'purpose': 'donation', 'source': 'bot'},
            purpose='donation',
            user_id=user_id,
            stars_title='Поддержать проект',
            stars_label='Пожертвование',
            stars_payload=f'balance_donation_{user_id}_{amount_kopeks}_{int(time.time())}',
            crypto_asset=settings.CRYPTOBOT_DEFAULT_ASSET,
            strict_metadata=True,
        )
