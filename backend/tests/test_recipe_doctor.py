"""Recipe doctor: contradictions found without AI, fixed by the AI only
where there are some; false alarms go on the tile's ignore list."""
import json

import pytest

from app import ignored, llm_provider, main, mealie_client, recipe_doctor, tool_jobs
from app.config import settings
from fake_mealie import FakeMealie


def ing(rid, food, amount=None, unit=None):
    return {"id": rid, "food": {"id": rid, "name": food, "plural_name": ""},
            "unit": {"id": 90, "name": unit} if unit else None, "amount": amount or 0, "no_amount": amount is None}


def recipe(**extra):
    r = {"id": 7, "name": "Gulasch", "servings": 4, "working_time": 20, "waiting_time": 10, "steps": [
        {"instruction": "Das Rindfleisch würfeln und mit den Zwiebeln scharf anbraten. Mit Sahne verfeinern.",
         "ingredients": [ing(1, "Rindfleisch", 800, "g"), ing(2, "Zwiebel", 3), ing(3, "Paprika", 2), ing(4, "Salz", 400, "g")]},
        {"instruction": "Zugedeckt 2 Stunden schmoren lassen.", "ingredients": []}]}
    r.update(extra)
    return r


def kinds(r, vocabulary=("Butter", "Sahne", "Rindfleisch")):
    return [f["kind"] + ":" + f["text"] for f in recipe_doctor.findings(r, vocabulary)]


def test_findings():
    found = kinds(recipe())
    assert "unused:Paprika is in no step" in found                       # Zwiebel matches "Zwiebeln"
    assert not any("Zwiebel is" in f or "Rindfleisch is" in f for f in found)
    assert "missing:the method uses Sahne, which isn't in the ingredients" in found
    assert any(f.startswith("amount:400 g Salz for 4 servings") for f in found)
    assert "time:the method takes about 120 min, the recipe says 30 min" in found


def test_a_consistent_recipe_has_none():
    r = recipe(working_time=20, waiting_time=120)
    r["steps"][0]["instruction"] = "Das Rindfleisch würfeln, mit Zwiebeln und Paprika anbraten und salzen."
    r["steps"][0]["ingredients"][3] = ing(4, "Salz", 1, "TL")
    assert kinds(r) == []
    r["steps"][0]["instruction"] = "{{ ingredients[0] }} würfeln."
    assert kinds(r) == []  # templates: nothing to compare


class AI:
    def __init__(self, monkeypatch, answer):
        self.answer, self.seen = answer, []
        monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
        monkeypatch.setattr(llm_provider, "complete_tool_text", self.ask)

    def ask(self, prompt, content, max_tokens=0, **kw):
        assert "contradictions" in prompt
        self.seen.append(json.loads(content))
        return json.dumps(self.answer), None


FIX = {"fixes": [
    {"type": "step_text", "n": 0, "text": "Das Rindfleisch würfeln und mit den Zwiebeln und der Paprika scharf anbraten. Mit Sahne verfeinern."},
    {"type": "amount", "key": "i3", "amount": 4, "unit": None},
    {"type": "add_ingredient", "n": 0, "food": "Sahne", "amount": 20, "unit": "g"},
    {"type": "times", "working_time": 20, "waiting_time": 120}], "false_alarms": []}


def test_the_fix_in_tandoor(tandoor, monkeypatch):
    ai = AI(monkeypatch, FIX)
    for i, n in [(1, "Rindfleisch"), (2, "Zwiebel"), (3, "Paprika"), (4, "Salz")]:
        tandoor.add("food", {"id": i, "name": n})
    tandoor.add("unit", {"id": 90, "name": "g"})
    tandoor.add("recipe", recipe())
    job = tool_jobs.create_tool_job("recipes_doctor")
    s = recipe_doctor.plan_suggestion(job, tandoor.db["recipe"][7], ["Sahne", "Rindfleisch"])
    assert ai.seen[0]["steps"][0]["ingredients"][3] == {"key": "i3", "text": "400 g Salz"}
    assert "400 g Salz -> 4 g Salz" in s.preview and "+ 20 g Sahne (step 1)" in s.preview
    job.suggestions = [s]
    tool_jobs.save_tool_job(job)
    assert main._perform_suggestion_action(job.id, s.id, "apply").status == "applied"
    saved = tandoor.db["recipe"][7]
    assert "der Paprika" in saved["steps"][0]["instruction"]
    assert [(i["food"]["name"], i["amount"]) for i in saved["steps"][0]["ingredients"]][-2:] == [("Salz", 4), ("Sahne", 20)]
    assert (saved["working_time"], saved["waiting_time"]) == (20, 120)


def test_unsafe_text_is_dropped(monkeypatch):
    AI(monkeypatch, {"fixes": [{"type": "step_text", "n": 1, "text": "Schmoren lassen."}], "false_alarms": []})
    plan = recipe_doctor.doctor_plan(tool_jobs.create_tool_job("recipes_doctor"), recipe(), [{"kind": "x", "text": "x"}])
    assert plan["fixes"] == []  # "2" got lost


def test_false_alarms_go_on_the_ignore_list(monkeypatch):
    AI(monkeypatch, {"fixes": [], "false_alarms": ["Paprika is in no step"]})
    assert recipe_doctor.plan_suggestion(tool_jobs.create_tool_job("recipes_doctor"), recipe(), ["Sahne"]) is None
    assert ignored.keys("recipes_inconsistent") == {"7"}


def test_mealie(monkeypatch):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    meat, onion, salt = fake.add("food", "Rindfleisch"), fake.add("food", "Zwiebel"), fake.add("food", "Salz")
    g = fake.add("unit", "g")
    r = fake.add_recipe("Gulasch", [(meat, g), (onion, None), (salt, g)], servings=4)
    r["recipeIngredient"][0]["quantity"], r["recipeIngredient"][2]["quantity"] = 800, 400
    r["recipeInstructions"] = [{"id": "s1", "title": "", "text": "Rindfleisch und Zwiebeln anbraten, salzen.",
                                "ingredientReferences": [{"referenceId": x["referenceId"]} for x in r["recipeIngredient"]]}]
    AI(monkeypatch, {"fixes": [{"type": "amount", "key": "i2", "amount": 4, "unit": None},
                               {"type": "add_ingredient", "n": 0, "food": "Paprika", "amount": 2, "unit": None}],
                     "false_alarms": []})
    from test_mealie_tools import apply_all, run
    job = run("recipes_doctor")
    assert len(job.suggestions) == 1
    apply_all(job)
    rows = fake.recipes["gulasch"]["recipeIngredient"]
    assert rows[2]["quantity"] == 4 and rows[3]["food"]["name"] == "Paprika"
    assert rows[3]["referenceId"] in [x["referenceId"] for x in fake.recipes["gulasch"]["recipeInstructions"][0]["ingredientReferences"]]
