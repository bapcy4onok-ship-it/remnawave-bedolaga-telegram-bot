"""Настройки донат-апселла: рубли из .env → копейки, отсев мусора в пресетах."""

from app.config import settings


def test_tip_kopeks_from_rubles(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_AMOUNT', 10, raising=False)
    assert settings.get_donation_upsell_tip_kopeks() == 1000


def test_tip_kopeks_accepts_fractional_rubles(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_AMOUNT', 10.5, raising=False)
    assert settings.get_donation_upsell_tip_kopeks() == 1050


def test_tip_kopeks_never_negative(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_AMOUNT', -5, raising=False)
    assert settings.get_donation_upsell_tip_kopeks() == 0


def test_presets_parsed_sorted_and_deduplicated(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_PRESETS', '100, 25,50,100, ,abc,0', raising=False)
    assert settings.get_donation_upsell_presets_kopeks() == [2500, 5000, 10000]


def test_presets_empty_string_gives_empty_list(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_PRESETS', '', raising=False)
    assert settings.get_donation_upsell_presets_kopeks() == []


def test_min_topup_kopeks(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_MIN_TOPUP', 300, raising=False)
    assert settings.get_donation_upsell_min_topup_kopeks() == 30000


def test_pledge_ttl_is_short_by_default():
    """TTL нужен не для оплаты (она идёт минуты), а чтобы забытое обещание не «прилипло»."""
    from app.config import Settings

    assert Settings.model_fields['DONATION_UPSELL_PLEDGE_TTL_HOURS'].default == 3
