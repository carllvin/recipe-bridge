"""Amounts into the steps: the AI only marks where an ingredient is named;
Tandoor gets its templates, Mealie the amounts written out."""
import json

import pytest

from app import llm_provider, mealie_client, recipe_amounts, tool_jobs
from app.config import settings
from fake_mealie import FakeMealie
from test_mealie_tools import apply_all, run


def ingredient(rid, food, amount=None, unit=None, plural=""):
    return {"id": rid, "food": {"id": rid, "name": food, "plural_name": plural},
            "unit": {"id": 1, "name": unit} if unit else None, "amount": amount or 0, "no_amount": amount is None}


def recipe():
    return {"id": 7, "name": "Pfannkuchen", "servings": 4, "working_time": 20, "steps": [
        {"instruction": "Das Mehl mit der Milch und den Eiern verrühren.", "ingredients": [
            ingredient(1, "Mehl", 250, "g"), ingredient(2, "Milch", 500, "ml"), ingredient(3, "Ei", 3, plural="Eier")]},
        {"instruction": "In der Pfanne bei 180 °C 3 Minuten je Seite backen.", "ingredients": [
            ingredient(4, "Butter", 1, "EL"), ingredient(5, "Salz")]},
    ]}


class AI:
    """answer: the amounts call; proofread: the check (default: all fine)."""

    def __init__(self, monkeypatch, answer, proofread=None):
        self.seen, self.checked, self.answer, self.proofread = [], [], answer, proofread
        monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
        monkeypatch.setattr(llm_provider, "complete_text", self.ask)
        monkeypatch.setattr(llm_provider, "complete_tool_text", self.check)

    def ask(self, prompt, content, max_tokens=0, **kw):
        assert "add the ingredient amounts" in prompt
        self.seen.append(json.loads(content))
        return json.dumps(self.answer), None

    def check(self, prompt, content, max_tokens=0, **kw):
        assert "You proofread cooking instructions" in prompt
        payload = json.loads(content)
        self.checked.append(payload)
        answer = self.proofread(payload) if self.proofread else {"steps": [{"ok": True} for _ in payload["steps"]]}
        return json.dumps(answer), None


GOOD = {"steps": ["[[i0]] mit [[i1]] und [[i2]] verrühren.", "[[i3]] in der Pfanne bei 180 °C 3 Minuten je Seite backen."]}


def test_which_recipes_need_it():
    assert recipe_amounts.needs_amounts(recipe())
    done = recipe()
    done["steps"][0]["instruction"] = "250 g Mehl mit 500 ml Milch und 3 Eiern verrühren."
    done["steps"][1]["instruction"] = "1 EL Butter erhitzen, bei 180 °C backen."
    assert not recipe_amounts.needs_amounts(done)
    templated = recipe()
    templated["steps"][0]["instruction"] = "{{ ingredients[0] }} verrühren."
    assert not recipe_amounts.needs_amounts(templated)
    one_block = recipe()
    one_block["steps"][1]["ingredients"] += one_block["steps"][0]["ingredients"]
    one_block["steps"][0]["ingredients"] = []
    one_block["steps"][1], one_block["steps"][0] = one_block["steps"][0], one_block["steps"][1]
    assert not recipe_amounts.needs_amounts(one_block)  # all in step 1 -> the structure revision first


def test_plan_keeps_the_text_and_never_writes_numbers(monkeypatch):
    ai = AI(monkeypatch, GOOD)
    job = tool_jobs.create_tool_job("recipes_amounts")
    suggestion = recipe_amounts.plan_suggestion(job, recipe())
    assert ai.seen[0]["steps"][0]["ingredients"] == [
        {"key": "i0", "text": "250 g Mehl"}, {"key": "i1", "text": "500 ml Milch"}, {"key": "i2", "text": "3 Eier"}]
    assert ai.seen[0]["steps"][1]["ingredients"][1] == {"key": "i4", "text": "Salz"}
    assert "AFTER:  250 g Mehl mit 500 ml Milch und 3 Eier verrühren." in suggestion.preview
    assert "AFTER:  1 EL Butter in der Pfanne" in suggestion.preview


@pytest.mark.parametrize("answer", [
    {"steps": ["[[i0]] verrühren.", "[[i3]] backen."]},                     # text dropped
    {"steps": ["[[i0]] mit [[i1]] und [[i2]] verrühren.", "[[i3]] in der Pfanne 3 Minuten je Seite backen."]},  # 180 lost
    {"steps": ["nur ein Schritt"]},                                          # wrong count
])
def test_untrustworthy_answers_are_dropped(monkeypatch, answer):
    AI(monkeypatch, answer)
    assert recipe_amounts.plan_suggestion(tool_jobs.create_tool_job("recipes_amounts"), recipe()) is None


def test_foreign_and_repeated_markers(monkeypatch):
    AI(monkeypatch, {"steps": ["[[i0]] mit [[i1]], [[i2]] und [[i0]] und [[i3]] verrühren.",
                               "In der Pfanne bei 180 °C 3 Minuten je Seite backen."]})
    plan = recipe_amounts.amounts_plan(tool_jobs.create_tool_job("recipes_amounts"), recipe())
    assert plan["steps"] == [{"index": 0, "text": "[[i0]] mit [[i1]], [[i2]] und Mehl und verrühren.",
                              "keys": {"i0": 1, "i1": 2, "i2": 3}, "ids": [1, 2, 3]}]


def test_tandoor_gets_templates(tandoor, monkeypatch):
    # step 2 lists Salz (i3) before Butter (i4) - the template counts the place in the step
    AI(monkeypatch, {"steps": [GOOD["steps"][0], "[[i4]] in der Pfanne bei 180 °C 3 Minuten je Seite backen."]})
    for i in range(1, 6):
        tandoor.db.setdefault("food", {})[i] = {"id": i, "name": ["", "Mehl", "Milch", "Ei", "Butter", "Salz"][i]}
    tandoor.db.setdefault("unit", {})[1] = {"id": 1, "name": "g"}
    r = recipe()
    r["steps"][1]["ingredients"].reverse()
    tandoor.add("recipe", r)
    job = tool_jobs.create_tool_job("recipes_amounts")
    tool_jobs.save_tool_job(job)
    job.suggestions = [recipe_amounts.plan_suggestion(job, tandoor.db["recipe"][7])]
    tool_jobs.save_tool_job(job)
    assert recipe_amounts.apply_suggestion(job.id, job.suggestions[0].id).status == "applied"
    steps = tandoor.db["recipe"][7]["steps"]
    assert steps[0]["instruction"] == "{{ ingredients[0] }} mit {{ ingredients[1] }} und {{ ingredients[2] }} verrühren."
    assert steps[1]["instruction"] == "{{ ingredients[1] }} in der Pfanne bei 180 °C 3 Minuten je Seite backen."
    assert [i["food"]["name"] for i in steps[0]["ingredients"]] == ["Mehl", "Milch", "Ei"]


def test_mealie_gets_the_amounts_written_out(monkeypatch):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    mehl, milch = fake.add("food", "Mehl"), fake.add("food", "Milch")
    g, ml = fake.add("unit", "g"), fake.add("unit", "ml")
    r = fake.add_recipe("Teig", [(mehl, g), (milch, ml)], servings=2)
    r["recipeIngredient"][0]["quantity"], r["recipeIngredient"][1]["quantity"] = 250, 0.5
    r["recipeInstructions"] = [{"id": "s1", "title": "", "text": "Das Mehl mit der Milch verrühren.",
                                "ingredientReferences": [{"referenceId": row["referenceId"]} for row in r["recipeIngredient"]]}]
    AI(monkeypatch, {"steps": ["[[i0]] mit [[i1]] verrühren."]})
    job = run("recipes_amounts")
    assert len(job.suggestions) == 1
    apply_all(job)
    assert fake.recipes["teig"]["recipeInstructions"][0]["text"] == "250 g Mehl mit 0,5 ml Milch verrühren."


def test_comments_in_brackets(monkeypatch, tandoor):
    r = recipe()
    r["steps"][0]["ingredients"][0]["note"] = "gesiebt"
    AI(monkeypatch, GOOD)
    job = tool_jobs.create_tool_job("recipes_amounts")
    suggestion = recipe_amounts.plan_suggestion(job, r)
    assert "AFTER:  250 g Mehl (gesiebt) mit 500 ml Milch und 3 Eier verrühren." in suggestion.preview

    # Tandoor: the comment through the template, only where there is one
    for i in range(1, 6):
        tandoor.db.setdefault("food", {})[i] = {"id": i, "name": ["", "Mehl", "Milch", "Ei", "Butter", "Salz"][i]}
    tandoor.db.setdefault("unit", {})[1] = {"id": 1, "name": "g"}
    tandoor.add("recipe", r)
    payload = recipe_amounts.build_payload(tandoor.db["recipe"][7], suggestion.detail["plan"])
    assert payload["steps"][0]["instruction"] == (
        "{{ ingredients[0] }} ({{ ingredients[0].note }}) mit {{ ingredients[1] }} und {{ ingredients[2] }} verrühren.")


def test_recipes_done_before_get_the_comments_without_ai(monkeypatch, tandoor):
    r = recipe()
    r["steps"][0]["instruction"] = "{{ ingredients[0] }} mit {{ ingredients[1] }} und {{ ingredients[2] }} verrühren."
    r["steps"][1]["instruction"] = "{{ ingredients[0] }} erhitzen, bei 180 °C backen."
    r["steps"][0]["ingredients"][0]["note"] = "gesiebt"
    assert recipe_amounts.needs_amounts(r)
    monkeypatch.setattr(llm_provider, "complete_text", lambda *a, **k: pytest.fail("no AI needed"))
    suggestion = recipe_amounts.plan_suggestion(tool_jobs.create_tool_job("recipes_amounts"), r)
    assert suggestion.summary.startswith("recipe: fix the ingredients in the steps")
    assert "AFTER:  250 g Mehl (gesiebt) mit 500 ml Milch und 3 Eier verrühren." in suggestion.preview
    assert [c["index"] for c in suggestion.detail["plan"]["steps"]] == [0]  # step 2: no comments

    for i in range(1, 6):
        tandoor.db.setdefault("food", {})[i] = {"id": i, "name": ["", "Mehl", "Milch", "Ei", "Butter", "Salz"][i]}
    tandoor.db.setdefault("unit", {})[1] = {"id": 1, "name": "g"}
    tandoor.add("recipe", r)
    payload = recipe_amounts.build_payload(tandoor.db["recipe"][7], suggestion.detail["plan"])
    text = payload["steps"][0]["instruction"]
    assert text == "{{ ingredients[0] }} ({{ ingredients[0].note }}) mit {{ ingredients[1] }} und {{ ingredients[2] }} verrühren."
    r["steps"][0]["instruction"] = text
    assert not recipe_amounts.needs_amounts(r)  # done


def plan_for(monkeypatch, first_step, proofread=None):
    ai = AI(monkeypatch, {"steps": [first_step, "In der Pfanne bei 180 °C 3 Minuten je Seite backen."]}, proofread)
    return ai, recipe_amounts.plan_suggestion(tool_jobs.create_tool_job("recipes_amounts"), recipe())


def test_articles_before_an_amount_are_dropped(monkeypatch):
    _ai, s = plan_for(monkeypatch, "Die [[i0]] mit der [[i1]] und den [[i2]] verrühren.")
    assert s.detail["plan"]["steps"][0]["text"] == "[[i0]] mit [[i1]] und [[i2]] verrühren."
    assert not s.detail.get("flagged")


def test_wrong_case_is_flagged_when_the_proofreading_misses_it(monkeypatch):
    _ai, s = plan_for(monkeypatch, "[[i0]] und [[i1]] mit [[i2]] verrühren.")  # "mit 3 Eier"
    assert s.detail["flagged"] and s.summary.startswith("⚠️ check 1 step(s)")
    assert "⚠️ „mit 3 Eier“" in s.preview


def test_the_proofreading_fix_is_used(monkeypatch):
    def proofread(payload):
        assert payload["steps"][0]["reads_as"] == "250 g Mehl und 500 ml Milch mit 3 Eier verrühren."
        return {"steps": [{"ok": False, "marked": "[[i0]], [[i1]] und [[i2]] verrühren.", "problem": "Fall"}]}
    ai, s = plan_for(monkeypatch, "[[i0]] und [[i1]] mit [[i2]] verrühren.", proofread)
    assert s.detail["plan"]["steps"][0]["text"] == "[[i0]], [[i1]] und [[i2]] verrühren."
    assert not s.detail.get("flagged") and "⚠️" not in s.preview


def test_an_unsafe_fix_is_not_used_but_flagged(monkeypatch):
    def proofread(payload):  # the "fix" drops most of the text
        return {"steps": [{"ok": False, "marked": "[[i0]].", "problem": "Satz holpert"}]}
    _ai, s = plan_for(monkeypatch, "[[i0]], [[i1]] und [[i2]] gut verrühren.", proofread)
    assert s.detail["plan"]["steps"][0]["text"] == "[[i0]], [[i1]] und [[i2]] gut verrühren."
    assert s.detail["flagged"] and "⚠️ Satz holpert" in s.preview


def test_proofreading_failing_does_not_stop_it(monkeypatch):
    ai = AI(monkeypatch, GOOD)
    monkeypatch.setattr(llm_provider, "complete_tool_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    s = recipe_amounts.plan_suggestion(tool_jobs.create_tool_job("recipes_amounts"), recipe())
    assert s is not None and not s.detail.get("flagged")


def test_flagged_suggestions_say_so_in_the_inbox(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    _ai, s = plan_for(monkeypatch, "[[i0]] und [[i1]] mit [[i2]] verrühren.")
    job = tool_jobs.create_tool_job("recipes_amounts")
    job.status = "ready"
    job.suggestions = [s]
    tool_jobs.save_tool_job(job)
    data = TestClient(main.app).get("/api/inbox").json()
    items = [i for g in data.get("groups", [data]) for i in g.get("items", [])] if isinstance(data, dict) else []
    item = next(i for i in items if i["id"] == s.id)
    assert item["flagged"] is True



def test_no_zero_pepper_in_new_templates(monkeypatch, tandoor):
    r = recipe()
    r["steps"][1]["ingredients"][1].update({"amount": 0, "no_amount": False, "note": "frisch gemahlen"})  # Salz, 0
    AI(monkeypatch, {"steps": [GOOD["steps"][0], "[[i3]] und [[i4]] in die Pfanne, bei 180 °C 3 Minuten je Seite backen."]})
    s = recipe_amounts.plan_suggestion(tool_jobs.create_tool_job("recipes_amounts"), r)
    assert "AFTER:  1 EL Butter und Salz (frisch gemahlen) in die Pfanne" in s.preview
    for i in range(1, 6):
        tandoor.db.setdefault("food", {})[i] = {"id": i, "name": ["", "Mehl", "Milch", "Ei", "Butter", "Salz"][i]}
    tandoor.db.setdefault("unit", {})[1] = {"id": 1, "name": "g"}
    tandoor.add("recipe", r)
    payload = recipe_amounts.build_payload(tandoor.db["recipe"][7], s.detail["plan"])
    assert payload["steps"][1]["instruction"].startswith("{{ ingredients[0] }} und Salz (frisch gemahlen) in die Pfanne")


def test_old_templates_reading_0_pepper_are_repaired(monkeypatch):
    r = recipe()
    r["steps"][1]["instruction"] = "{{ ingredients[0] }} und {{ ingredients[1] }} in die Pfanne geben."
    r["steps"][1]["ingredients"][1].update({"amount": 0, "no_amount": False})
    assert recipe_amounts.needs_amounts(r)
    monkeypatch.setattr(llm_provider, "complete_text", lambda *a, **k: pytest.fail("no AI needed"))
    s = recipe_amounts.plan_suggestion(tool_jobs.create_tool_job("recipes_amounts"), r)
    assert "BEFORE: 1 EL Butter und 0 Salz in die Pfanne geben." in s.preview
    assert "AFTER:  1 EL Butter und Salz in die Pfanne geben." in s.preview
    fixed = s.detail["plan"]["steps"][0]["text"]
    assert fixed == "{{ ingredients[0] }} und Salz in die Pfanne geben."
    r["steps"][1]["instruction"] = fixed
    assert not recipe_amounts.needs_amounts(r)
