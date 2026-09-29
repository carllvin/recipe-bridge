"""Planning with Mealie: weekly plan, "What can I cook today?", "How was it?"."""
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import cook_feedback, cook_today, llm_provider, main, mealie_client, mealie_plan, tool_jobs, tools_meal_plan
from app.config import settings
from fake_mealie import FakeMealie


@pytest.fixture
def mealie(monkeypatch):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(settings, "output_language", "Deutsch")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    zucchini, feta, rice = fake.add("food", "Zucchini"), fake.add("food", "Feta"), fake.add("food", "Reis")
    fake.add_recipe("Zucchinipfanne", [(zucchini, None), (feta, None)], servings=2, totalTime="25 min", rating=5)
    fake.add_recipe("Risotto", [(rice, None)], servings=4, prepTime="10 min", performTime="30 Minuten")
    recent = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    fake.add_recipe("Gerade gekocht", [(rice, None)], lastMade=f"{recent}T19:00:00")
    return fake


def by_name(fake, name):
    return next(r for r in fake.recipes.values() if r["name"] == name)


def test_minutes():
    assert mealie_plan.minutes({"totalTime": "1 hour 30 minutes"}) == 90
    assert mealie_plan.minutes({"totalTime": "PT1H15M"}) == 75
    assert mealie_plan.minutes({"totalTime": "1:05"}) == 65
    assert mealie_plan.minutes({"prepTime": "10 min", "performTime": "1 Stunde"}) == 70
    assert mealie_plan.minutes({"totalTime": "etwas"}) == 0


def test_options_are_mealies_entry_types(mealie):
    types = tools_meal_plan.options()["meal_types"]
    assert {"id": "dinner", "name": "Abendessen"} in types


def test_weekly_plan_reads_mealie_and_writes_plan_and_shopping_list(mealie, monkeypatch):
    start = dt.date.today() + dt.timedelta(days=1)
    mealie.add_mealplan((start + dt.timedelta(days=1)).isoformat(), "dinner", by_name(mealie, "Risotto"))
    seen = {}

    def fake_ai(prompt, content, max_tokens=0):
        data = json.loads(content)
        seen.update(data)
        ids = [line.split("|")[0] for line in data["recipes"]]
        return json.dumps([{"date": d.split(" ")[0], "recipe_id": ids[i % len(ids)], "reason": "passt"}
                           for i, d in enumerate(data["days"])]), None
    monkeypatch.setattr(llm_provider, "complete_tool_text", fake_ai)
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)

    job = tool_jobs.create_tool_job("meal_plan")
    job.meta["params"] = {"start_date": start.isoformat(), "days": 3, "meal_type": {"id": "dinner", "name": "Abendessen"},
                          "add_to_shopping": True}
    tool_jobs.save_tool_job(job)
    tools_meal_plan.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert job.status == "ready", job.error
    assert job.meta["taken_days"] == [(start + dt.timedelta(days=1)).isoformat()]
    # recently cooked recipes are left out; times come from Mealie's free text
    assert not any("Gerade gekocht" in line for line in seen["recipes"])
    assert any(line.startswith(by_name(mealie, "Zucchinipfanne")["id"] + "|Zucchinipfanne||25|5|") for line in seen["recipes"])
    assert len(job.suggestions) == 2

    s = job.suggestions[0]
    tools_meal_plan.apply_suggestion(job.id, s.id)
    assert tool_jobs.get_tool_job(job.id).suggestions[0].status == "applied"
    entry = mealie.mealplans[-1]
    assert (entry["date"], entry["entryType"], entry["recipeId"]) == (s.detail["date"], "dinner", s.detail["recipe"]["id"])
    # no shopping list yet -> one was created and the recipe put on it
    assert [lst["recipes"] for lst in mealie.shopping_lists] == [[s.detail["recipe"]["id"]]]


def test_cook_today_index_and_plan_today(mealie, isolated):
    cook_today._rebuild()
    results = cook_today.suggest("Zucchini")["results"]
    assert [r["name"] for r in results] == ["Zucchinipfanne"]
    assert results[0]["slug"] == "zucchinipfanne" and results[0]["missing"] == ["Feta"]

    client = TestClient(main.app)
    res = client.post("/api/cook-today/plan", json={"recipe": {"id": results[0]["id"], "name": "Zucchinipfanne"},
                                                    "meal_type": {"id": "lunch", "name": "Mittagessen"}})
    assert res.status_code == 200, res.text
    assert mealie.mealplans[-1]["entryType"] == "lunch" and mealie.shopping_lists == []


def test_how_was_it_sets_rating_and_last_made(mealie, isolated):
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    risotto = by_name(mealie, "Risotto")
    mealie.add_mealplan(yesterday, "dinner", risotto)
    # made after the planned day -> counts as answered in Mealie already
    mealie.add_mealplan(yesterday, "lunch", by_name(mealie, "Gerade gekocht") | {"lastMade": f"{dt.date.today()}T12:00:00"})

    client = TestClient(main.app)
    items = client.get("/api/cooked/pending").json()["items"]
    assert [(i["recipe"]["name"], i["meal_type"]) for i in items] == [("Risotto", "Abendessen")]

    res = client.post("/api/cooked", json={"plan_id": items[0]["plan_id"], "recipe_id": risotto["id"],
                                           "date": yesterday, "servings": 4, "rating": 4})
    assert res.status_code == 200, res.text
    assert mealie.ratings == {risotto["id"]: 4}
    assert risotto["lastMade"].startswith(yesterday)
    assert client.get("/api/cooked/pending").json()["items"] == []


def test_not_cooked_writes_nothing(mealie, isolated):
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    risotto = by_name(mealie, "Risotto")
    mealie.add_mealplan(yesterday, "dinner", risotto)
    cook_feedback.answer(1, risotto["id"], yesterday, 4, None)
    assert mealie.ratings == {} and not risotto.get("lastMade")
    assert cook_feedback.pending() == []
