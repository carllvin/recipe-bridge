"""Weekly plan: freshness order, what's at home, and changing the plan in a chat."""
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import cook_today, llm_provider, main, mealie_client, perishability, tool_jobs, tools_meal_plan
from app.config import settings
from fake_mealie import FakeMealie


def test_perishability():
    assert perishability.level_of("Lachsfilet") == 3 and perishability.level_of("Blattspinat") == 3
    assert perishability.level_of("Rinderhack") == 3 and perishability.level_of("Hähnchenbrust") == 2
    for keeps in ("Fischsauce", "TK-Spinat", "Thunfisch aus der Dose", "Tomatenmark", "Linsen", "Rinderbrühe"):
        assert perishability.level_of(keeps) == 0, keeps
    assert perishability.of_recipe(["Lachs", "Zitrone", "Spinat", "Reis"]) == (3, ["Lachs", "Spinat"])
    assert perishability.of_recipe(["Reis", "Linsen"]) == (0, [])


@pytest.fixture
def mealie(monkeypatch, isolated):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    food = {name: fake.add("food", name) for name in ("Lachs", "Spinat", "Linsen", "Rindfleisch", "Nudeln", "Feta")}
    fake.add_recipe("Lachs mit Spinat", [(food["Lachs"], None), (food["Spinat"], None)], totalTime="25 min")
    fake.add_recipe("Linsensuppe", [(food["Linsen"], None)], totalTime="40 min")
    fake.add_recipe("Rindergulasch", [(food["Rindfleisch"], None)], totalTime="120 min")
    fake.add_recipe("Nudeln mit Feta", [(food["Nudeln"], None), (food["Feta"], None)], totalTime="20 min")
    cook_today._rebuild()
    return fake


def ids(fake):
    return {r["name"]: r["id"] for r in fake.recipes.values()}


class AI:
    def __init__(self, monkeypatch):
        self.seen, self.answer = [], None
        monkeypatch.setattr(llm_provider, "complete_tool_text", self.ask)

    def ask(self, prompt, content, max_tokens=0):
        payload = json.loads(content)
        self.seen.append((prompt, payload))
        return json.dumps(self.answer(payload)), None


def make_plan(fake, ai, days=3):
    by_name = ids(fake)
    start = dt.date.today() + dt.timedelta(days=1)
    ai.answer = lambda p: [{"date": d.split(" ")[0], "recipe_id": by_name[name], "reason": "passt"}
                           for d, name in zip(p["days"], ["Lachs mit Spinat", "Nudeln mit Feta", "Linsensuppe"])]
    job = tool_jobs.create_tool_job("meal_plan")
    job.meta["params"] = {"start_date": start.isoformat(), "days": days, "meal_type": {"id": "dinner", "name": "Abendessen"},
                          "at_home": "Spinat, Feta, Salz"}
    tool_jobs.save_tool_job(job)
    tools_meal_plan.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert job.status == "ready", job.error
    return job


def test_plan_lines_show_perishable_and_at_home(mealie, monkeypatch):
    ai = AI(monkeypatch)
    make_plan(mealie, ai)
    prompt, payload = ai.seen[0]
    assert "shopping day" in prompt and "perishable" in prompt
    assert payload["at_home"] == ["Spinat", "Feta", "Salz"]
    lines = {line.split("|")[1]: line.split("|") for line in payload["recipes"]}
    assert lines["Lachs mit Spinat"][7] == "3:Lachs,Spinat" and lines["Lachs mit Spinat"][8] == "Spinat"
    assert lines["Linsensuppe"][7] == "-" and lines["Linsensuppe"][8] == "-"
    assert lines["Nudeln mit Feta"][8] == "Feta"


def test_chat_swaps_and_replaces_days(mealie, monkeypatch):
    ai = AI(monkeypatch)
    job = make_plan(mealie, ai)
    by_name = ids(mealie)
    days = job.meta["days"]
    tools_meal_plan.apply_suggestion(job.id, job.suggestions[0].id)  # day 1 is in the meal plan now

    ai.answer = lambda p: {"reply": "Donnerstag gibt's Gulasch.", "changes": [
        {"date": days[2], "recipe_id": by_name["Rindergulasch"], "reason": "mit Rind"},
        {"date": days[0], "recipe_id": by_name["Linsensuppe"], "reason": "x"},   # locked - ignored
        {"date": days[1], "recipe_id": "unknown", "reason": "x"},                # unknown recipe - ignored
    ]}
    res = TestClient(main.app).post(f"/api/tools/meal-plan/{job.id}/chat", json={"message": "Donnerstag etwas mit Rind"})
    assert res.status_code == 200, res.text
    data = res.json()
    plan = {s["detail"]["date"]: (s["detail"]["recipe"]["name"], s["status"]) for s in data["suggestions"]}
    assert plan == {days[0]: ("Lachs mit Spinat", "applied"), days[1]: ("Nudeln mit Feta", "pending"),
                    days[2]: ("Rindergulasch", "pending")}
    assert len(data["meta"]["last_changed"]) == 1
    assert data["meta"]["chat"][-1]["text"] == "Donnerstag gibt's Gulasch." and data["meta"]["chat"][-1]["changed"] == 1
    prompt, payload = ai.seen[-1]
    assert payload["message"] == "Donnerstag etwas mit Rind"
    assert [d["locked"] for d in payload["plan"]] == [True, False, False]

    # a swap of two days; the earlier exchange goes along as history
    ai.answer = lambda p: {"reply": "Getauscht.", "changes": [
        {"date": days[1], "recipe_id": by_name["Rindergulasch"]}, {"date": days[2], "recipe_id": by_name["Nudeln mit Feta"]}]}
    job = tools_meal_plan.chat(job.id, "Tausch die letzten zwei Tage")
    assert [s.detail["recipe"]["name"] for s in job.suggestions] == ["Lachs mit Spinat", "Rindergulasch", "Nudeln mit Feta"]
    assert ai.seen[-1][1]["history"][0]["text"] == "Donnerstag etwas mit Rind"
    assert len(job.meta["chat"]) == 4


def test_chat_swaps_instead_of_planning_a_recipe_twice(mealie, monkeypatch):
    ai = AI(monkeypatch)
    job = make_plan(mealie, ai)
    days, by_name = job.meta["days"], ids(mealie)
    # "Thursday the lentil soup" - which is already on day 3: day 1's recipe moves there
    ai.answer = lambda p: {"reply": "ok", "changes": [{"date": days[0], "recipe_id": by_name["Linsensuppe"]}]}
    job = tools_meal_plan.chat(job.id, "Tag 1 Linsensuppe")
    assert [s.detail["recipe"]["name"] for s in job.suggestions] == ["Linsensuppe", "Nudeln mit Feta", "Lachs mit Spinat"]
    assert len(job.meta["last_changed"]) == 2


def test_chat_needs_a_finished_plan(mealie):
    job = tool_jobs.create_tool_job("meal_plan")
    res = TestClient(main.app).post(f"/api/tools/meal-plan/{job.id}/chat", json={"message": "hallo"})
    assert res.status_code == 400
