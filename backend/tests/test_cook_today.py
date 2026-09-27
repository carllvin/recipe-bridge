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
    assert found("Zucchini, Feta") == [("Zucchini-Feta-Pfanne", ["Knoblauch"])]
    assert found("Tomate, Reis, Zwiebeln") == [("Tomatenreis", [])]


def test_short_words_only_match_whole_names(index):
    assert found("Ei") == []


def test_staples_can_count_as_missing(index):
    assert found("Zucchini, Feta", staples=False) == [("Zucchini-Feta-Pfanne", ["Olivenöl", "Meersalz", "Knoblauch"])]


def test_fewest_missing_first(index):
    names = [n for n, _ in found("Zucchini, Tomate, Reis, Zwiebel")]
    assert names == ["Tomatenreis", "Zucchini-Feta-Pfanne"]
