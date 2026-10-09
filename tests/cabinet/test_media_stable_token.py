"""Стабильные токены медиа для очереди фотоотчётов в админке.

Обычный токен меняется каждую секунду, и каждый перезапрос очереди (после
одобрения, смены фильтра) давал новые ссылки на картинки — браузер заново качал
все превью через Telegram. Стабильный токен одинаков в пределах 6-часового окна
и действует не меньше обычных 24 часов.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cabinet.routes import media


FILE_A = 'AgACAgIAAxkBAAIBQ2ZxAAAAAAAAAAAAAAAAAAAA'
FILE_B = 'AgACAgIAAxkBAAIBQ2ZxBBBBBBBBBBBBBBBBBBBB'
BUCKET = 6 * 60 * 60
TTL = 24 * 60 * 60
WINDOW_START = 1_800_000_000 - 1_800_000_000 % BUCKET  # начало 6-часового окна


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(now=WINDOW_START + 1)
    monkeypatch.setattr(media, 'time', SimpleNamespace(time=lambda: state.now))
    return state


def _exp(token: str) -> int:
    return int(token.split('.', 1)[0])


def test_same_token_within_one_window_and_verifier_accepts_it(clock):
    first = media.make_stable_media_token(FILE_A)
    clock.now = WINDOW_START + BUCKET - 1  # последняя секунда того же окна
    second = media.make_stable_media_token(FILE_A)

    assert second == first
    assert media._verify_media_token(FILE_A, first)
    assert not media._verify_media_token(FILE_B, first)  # подпись привязана к файлу
    # Действует не меньше обычного TTL — даже выданный в конце окна.
    assert _exp(first) - clock.now >= TTL
    assert _exp(first) - (WINDOW_START + 1) <= TTL + BUCKET


def test_next_window_gives_new_token_and_old_one_still_works_until_expiry(clock):
    old = media.make_stable_media_token(FILE_A)
    clock.now = WINDOW_START + BUCKET + 1

    new = media.make_stable_media_token(FILE_A)

    assert new != old
    assert _exp(new) == _exp(old) + BUCKET
    assert media._verify_media_token(FILE_A, old)  # уже открытая страница не ломается
    clock.now = _exp(old) + 1
    assert not media._verify_media_token(FILE_A, old)
    assert media._verify_media_token(FILE_A, new)


def test_regular_token_is_unchanged(clock):
    """Обычные токены (тикеты) по-прежнему живут ровно 24 часа от выдачи."""
    token = media.make_media_token(FILE_A)
    assert _exp(token) == clock.now + TTL
    clock.now += 1
    assert media.make_media_token(FILE_A) != token
