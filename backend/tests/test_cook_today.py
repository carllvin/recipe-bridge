import pytest

from app import cook_today


def recipe(rid, name, foods, rating=None):
    return {"id": rid, "name": name, "rating": rating,
            "steps": [{"ingredients": [{"food": {"id": i, "name": f[0], "plural_name": f[1]}} for i, f in enumerate(foods)]}]}


@pytest.fixture
def index(monkeypatch):
    recipes = [
        recipe(1, "Zucchini-Feta-Pfanne", [("Zucchini", ""), ("Feta-Käse", ""), ("Olivenöl", ""), ("Meersalz", ""), ("Knoblauch", "")], 4),
        recipe(2, "Tomatenreis", [("Cherrytomaten", ""), ("Reis", ""), ("Zwiebel", "Zwiebeln")]),
        recipe(3, "Eis", [("Eis", ""), ("Sahne", "")]),
    ]
    data = {"built_at": 9e18, "recipes": cook_today.build_index(recipes)}
    monkeypatch.setattr(cook_today, "_load", lambda: data)


def found(have, **kw):
    return [(r["name"], r["missing"]) for r in cook_today.suggest(have, **kw)["results"]]


def test_forgiving_matches(index):
    # Olivenöl, Meersalz and Knoblauch are staples - nothing is missing
    assert found("Zucchini, Feta") == [("Zucchini-Feta-Pfanne", [])]
    assert found("Tomate, Reis") == [("Tomatenreis", [])]


def test_short_words_only_match_whole_names(index):
    assert found("Ei") == []


def test_staples_are_always_at_home():
    assert cook_today._is_staple(["Olivenöl", ""]) and cook_today._is_staple(["Meersalz", ""])
    assert cook_today._is_staple(["Rote Zwiebel", "Rote Zwiebeln"]) and cook_today._is_staple(["Knoblauchzehe", ""])
    assert not cook_today._is_staple(["Frühlingszwiebel", ""])
    assert not cook_today._is_staple(["Zucchini", ""])


def test_fewest_missing_first(index):
    names = [n for n, _ in found("Zucchini, Tomate")]
    assert names == ["Zucchini-Feta-Pfanne", "Tomatenreis"]  # 1 missing (Feta) vs. 1 missing (Reis), rating decides
