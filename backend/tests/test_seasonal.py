"""Seasonal calendar: matching produce, ranking in 'what can I cook today?'
and the weekly plan's recipe lines."""
import datetime as dt
import json

from app import cook_today, llm_provider, seasonal, tools_meal_plan
from app.schemas import ToolJob


def names(keys):
    return [seasonal.PRODUCE[k][0] for k in keys]


def test_stems_and_exclusions():
    assert names(seasonal.seasonal_in(["Butternutkürbis", "Kürbiskerne", "Schnittlauch", "Porree"], 10)) == ["Kürbis", "Lauch"]
    # cherry tomatoes are tomatoes (not cherries), chickpeas and frozen peas don't count
    assert set(names(seasonal.seasonal_in(["Kirschtomaten", "Kichererbsen", "Erbsen (TK)", "Zuckerschoten"], 7))) == {"Erbsen", "Tomaten"}
    assert names(seasonal.seasonal_in(["Apfelessig", "Grapefruit"], 9)) == []
    assert seasonal.seasonal_in(["Spargel"], 5) and not seasonal.seasonal_in(["Spargel"], 11)


def test_every_month_has_produce_and_known_keys():
    for month in range(1, 13):
        assert seasonal.in_season(month) and all(k in seasonal.PRODUCE for k in seasonal.in_season(month))


def _index(monkeypatch, recipes):
    data = {"built_at": 9e18, "recipes": cook_today.build_index(recipes)}
    monkeypatch.setattr(cook_today, "_load", lambda: data)


def recipe(rid, name, foods, rating=None):
    return {"id": rid, "name": name, "rating": rating,
            "steps": [{"ingredients": [{"food": {"id": i, "name": f}} for i, f in enumerate(foods)]}]}


def test_seasonal_recipes_rank_higher(monkeypatch):
    month = dt.date.today().month
    produce = seasonal.PRODUCE[seasonal.in_season(month)[0]][0]
    _index(monkeypatch, [recipe(1, "Ohne Saison", ["Reis", "Feta"], rating=5),
                         recipe(2, "Mit Saison", ["Reis", produce], rating=3)])
    results = cook_today.suggest("Reis")["results"]
    # both miss one ingredient and use one of yours - the seasonal one wins despite the rating
    assert [r["name"] for r in results] == ["Mit Saison", "Ohne Saison"]
    assert results[0]["season"]


def test_weekly_plan_lines_mention_seasonal_ingredients(monkeypatch):
    month = dt.date.today().month
    produce = seasonal.PRODUCE[seasonal.in_season(month)[0]][0]
    _index(monkeypatch, [recipe(1, "Saisonsuppe", [produce])])
    seen = {}

    def fake(prompt, content, max_tokens=0):
        seen.update(json.loads(content))
        return "[]", None
    monkeypatch.setattr(llm_provider, "complete_tool_text", fake)
    tools_meal_plan._pick(ToolJob(id="j", tool="meal_plan"), [{"id": 1, "name": "Saisonsuppe", "keywords": []}],
                          [dt.date.today()], {"meal_type": {"id": 1, "name": "Abendessen"}})
    assert seen["recipes"][0].endswith("|" + produce) and seen["in_season"]
