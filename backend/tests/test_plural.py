import pytest

from app.tools_ingredients import plausible_plural


@pytest.mark.parametrize("name, plural", [
    ("Koreanische Chilipaste", "Koreanische Chilipasten"),
    ("Sojasauce", "Sojasaucen"),
    ("Sesamöl", "Sesamöle"),
    ("Tomatenmark", "Tomatenmarke"),
    ("Zucker", "Zucker"),          # same as the singular
    ("Mehl", ""),
])
def test_nonsense_plurals_are_dropped(name, plural):
    assert plausible_plural(name, plural) == ""


@pytest.mark.parametrize("name, plural", [
    ("Tomate", "Tomaten"),
    ("Rote Zwiebel", "Rote Zwiebeln"),
    ("Wassermelone", "Wassermelonen"),   # contains "wasser", but isn't a mass noun
    ("Gewürzgurke", "Gewürzgurken"),     # starts with "gewürz"
])
def test_countable_plurals_are_kept(name, plural):
    assert plausible_plural(name, plural) == plural


def test_mass_noun_filter_only_applies_to_german(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "output_language", "English")
    assert plausible_plural("Chili paste", "Chili pastes") == "Chili pastes"
