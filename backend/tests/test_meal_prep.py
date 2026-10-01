"""One work plan for several recipes: the weekly plan's days (meal prep) or
a guest menu (clock times back from "ready at")."""
import json

import pytest
from fastapi.testclient import TestClient

from app import llm_provider, main, tool_jobs
from app.schemas import ToolSuggestion

PLAN = {"total_minutes": 95, "phases": [
    {"title": "Vorbereiten", "time": "17:25", "tasks": [
        {"text": "3 Zwiebeln würfeln (Suppe und Curry)", "recipes": ["Suppe", "Curry", "Erfunden"], "minutes": 10}]},
    {"title": "Kochen", "time": "kurz", "tasks": [{"text": "", "recipes": []}, {"text": "Suppe köcheln", "recipes": ["Suppe"]}]},
    {"title": "leer", "tasks": []}], "keeping": ["Suppe hält 3 Tage im Kühlschrank"]}


@pytest.fixture
def ai(monkeypatch):
    seen = []
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)

    def ask(prompt, content, max_tokens=0, **kw):
        assert "ONE work plan" in prompt
        seen.append(json.loads(content))
        return json.dumps(PLAN), None
    monkeypatch.setattr(llm_provider, "complete_tool_text", ask)
    return seen


def kitchen(tandoor):
    tandoor.add("food", {"id": 1, "name": "Zwiebel"})
    tandoor.add("unit", {"id": 2, "name": "g"})
    for rid, name in [(10, "Suppe"), (11, "Curry"), (12, "Salat")]:
        tandoor.add("recipe", {"id": rid, "name": name, "servings": 2, "steps": [
            {"instruction": f"{name}: Zwiebel würfeln.", "time": 5,
             "ingredients": [{"food": {"id": 1}, "unit": {"id": 2}, "amount": 100}]}]})


def test_meal_prep_for_the_chosen_days(tandoor, ai):
    kitchen(tandoor)
    job = tool_jobs.create_tool_job("meal_plan")
    job.suggestions = [ToolSuggestion(id=f"s{rid}", kind="meal_plan", summary="", status=status,
                                      detail={"date": f"2026-10-0{n}", "recipe": {"id": rid, "name": name}})
                       for n, (rid, name, status) in enumerate([(11, "Curry", "pending"), (10, "Suppe", "pending"),
                                                                 (12, "Salat", "skipped")], 3)]
    tool_jobs.save_tool_job(job)
    from app import app_settings
    app_settings.update({"household": {"persons": 4}})
    data = TestClient(main.app).post("/api/meal-prep", json={"job_id": job.id}).json()
    sent = ai[0]
    assert sent["mode"] == "prep" and [r["name"] for r in sent["recipes"]] == ["Curry", "Suppe"]  # skipped day out
    assert sent["recipes"][0]["scale"] == 2 and sent["recipes"][0]["ingredients"] == ["200 g Zwiebel"]
    assert data["phases"] == [
        {"title": "Vorbereiten", "time": "17:25", "tasks": [
            {"text": "3 Zwiebeln würfeln (Suppe und Curry)", "recipes": ["Suppe", "Curry"], "minutes": 10}]},
        {"title": "Kochen", "time": None, "tasks": [{"text": "Suppe köcheln", "recipes": ["Suppe"], "minutes": None}]}]
    assert tool_jobs.get_tool_job(job.id).meta["prep"]["keeping"] == ["Suppe hält 3 Tage im Kühlschrank"]


def test_work_plan_for_a_guest_menu(tandoor, ai):
    kitchen(tandoor)
    job = tool_jobs.create_tool_job("guest_menu")
    job.meta = {"params": {"persons": 8, "date": "2026-10-10"},
                "menu": [{"course": "starter", "recipe": {"id": 12, "name": "Salat"}},
                         {"course": "main", "recipe": {"id": 11, "name": "Curry"}}]}
    tool_jobs.save_tool_job(job)
    TestClient(main.app).post("/api/meal-prep", json={"job_id": job.id, "ready_at": "19:30"})
    assert ai[0]["mode"] == "menu" and ai[0]["ready_at"] == "19:30" and ai[0]["persons"] == 8
    assert ai[0]["recipes"][1]["scale"] == 4


def test_unknown_run():
    assert TestClient(main.app).post("/api/meal-prep", json={"job_id": "nope"}).status_code == 400


def test_oven_similar_temperatures_share_one_with_adjusted_time():
    from app import meal_prep
    recipes = [{"name": "Lasagne", "steps": [{"text": "Bei 180 °C 40 Minuten backen.", "minutes": None}]},
               {"name": "Kuchen", "steps": [{"text": "Im Ofen bei 160 Grad Umluft 45 Min. backen.", "minutes": None}]},
               {"name": "Gratin", "steps": [{"text": "Bei 200° überbacken.", "minutes": 30}]},
               {"name": "Pizza", "steps": [{"text": "Bei 250 °C 10 Minuten backen.", "minutes": None}]},
               {"name": "Salat", "steps": [{"text": "Mischen.", "minutes": None}]}]
    uses = meal_prep.oven_uses(recipes)
    assert [u["temp"] for u in uses] == [180, 180, 200, 250]  # fan + 20
    plan = meal_prep.oven_plan(uses)
    assert [g["temperature"] for g in plan] == [185, 250]  # close ones averaged, the far one after
    first = {d["recipe"]: d["minutes"] for d in plan[0]["dishes"]}
    assert first == {"Lasagne": 39, "Kuchen": 44, "Gratin": 32}  # hotter -> shorter, cooler -> longer
    assert plan[1]["dishes"][0]["minutes"] == 10


def test_oven_warning_for_two_preheat_temperatures():
    from app import meal_prep
    assert meal_prep._oven_warning({"tasks": [{"text": "Ofen auf 180 °C vorheizen"},
                                              {"text": "Zweiten Ofen auf 250 °C vorheizen"}]})
    assert not meal_prep._oven_warning({"tasks": [{"text": "Ofen auf 180 °C vorheizen"},
                                                  {"text": "Bei 190 °C backen"}]})


def test_meal_prep_sends_the_oven_plan(tandoor, ai):
    tandoor.add("recipe", {"id": 20, "name": "Brot", "servings": 2,
                           "steps": [{"instruction": "Bei 230 °C 35 Minuten backen.", "ingredients": []}]})
    job = tool_jobs.create_tool_job("meal_plan")
    job.suggestions = [ToolSuggestion(id="s20", kind="meal_plan", summary="", status="pending",
                                      detail={"date": "2026-10-03", "recipe": {"id": 20, "name": "Brot"}})]
    tool_jobs.save_tool_job(job)
    data = TestClient(main.app).post("/api/meal-prep", json={"job_id": job.id}).json()
    assert ai[0]["oven"] == [{"temperature": 230, "dishes": [
        {"recipe": "Brot", "step": "Bei 230 °C 35 Minuten backen.", "original": "230 °C, 35 min", "minutes": 35}]}]
    assert data["oven"] == ai[0]["oven"]
