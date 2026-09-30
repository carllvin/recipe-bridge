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
    def __init__(self, monkeypatch, answer):
        self.seen, self.answer = [], answer
        monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
        monkeypatch.setattr(llm_provider, "complete_text", self.ask)

    def ask(self, prompt, content, max_tokens=0, **kw):
        assert "add the ingredient amounts" in prompt
        self.seen.append(json.loads(content))
        return json.dumps(self.answer), None


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
