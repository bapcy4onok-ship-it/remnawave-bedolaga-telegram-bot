"""Пользовательские локали (`./locales`) перекрывают дефолтные — и это легко проглядеть.

`app/localization/loader.py::load_locale` мержит `app/localization/locales/<lang>.json`
(дефолт из образа) с `LOCALES_PATH/<lang>.json` (примонтированный каталог, правится
на живом сервере), и ПОБЕЖДАЕТ пользовательский файл. Поэтому правка текста только в
дефолтах до людей не доезжает, если тот же ключ есть в override.

Тесты ниже ловят именно это: расхождение плейсхолдеров (сломает `.format`) и
конкретную фразу про Stars, на которой мы уже один раз обожглись.
"""

import json
import re
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT_DIR / 'app' / 'localization' / 'locales'
OVERRIDE_DIR = ROOT_DIR / 'locales'
LANGUAGES = ('ru', 'en', 'ua', 'fa', 'zh')

# Расхождения, которые существовали до появления этого теста. Не «разрешено навсегда»,
# а зафиксированный долг: override сознательно короче дефолта.
KNOWN_PLACEHOLDER_DRIFT = {('en', 'PROMOCODE_SUCCESS')}


def _placeholders(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    return set(re.findall(r'\{(\w+)\}', value))


def _load(directory: Path, language: str) -> dict:
    path = directory / f'{language}.json'
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


@pytest.mark.parametrize('language', LANGUAGES)
def test_override_placeholders_match_defaults(language):
    """Ключ из override получит те же .format(...), что и дефолтный — иначе KeyError."""
    defaults = _load(DEFAULT_DIR, language)
    overrides = _load(OVERRIDE_DIR, language)

    drifted = {
        key
        for key in set(defaults) & set(overrides)
        if _placeholders(defaults[key]) != _placeholders(overrides[key])
        and (language, key) not in KNOWN_PLACEHOLDER_DRIFT
    }

    assert not drifted, f'{language}: плейсхолдеры override разошлись с дефолтом: {sorted(drifted)}'


@pytest.mark.parametrize('language', LANGUAGES)
def test_stars_success_does_not_promise_credited_amount(language):
    """При надбавке донат-апселла платёж больше того, что осело на балансе.

    Сообщение об оплате Stars уходит уже после списания надбавки, поэтому фраза
    «зачислено на баланс» врала бы на сумму надбавки. Правка нужна В ОБОИХ файлах:
    в дефолтном её просто не видно, пока ключ есть в override.
    """
    forbidden = {
        'ru': 'Зачислено на баланс',
        'en': 'Added to balance',
        'ua': 'Зараховано на баланс',
        'fa': 'واریز به موجودی',
        'zh': '已存入余额',
    }[language]

    for directory in (DEFAULT_DIR, OVERRIDE_DIR):
        value = _load(directory, language).get('STARS_PAYMENT_SUCCESS')
        if value is None:
            continue
        assert forbidden not in value, f'{directory.name}/{language}: сумма платежа названа зачисленной'
