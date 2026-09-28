"""Almost-JSON answers from the AI are repaired or asked for once more."""
import json

import pytest

from app import json_answer, tools_recipes
from app.schemas import TokenUsage


@pytest.mark.parametrize("raw, expected", [
    ('{"t": "Carrots with a "spicy" dressing", "n": 1}', {"t": 'Carrots with a "spicy" dressing', "n": 1}),
    ('```json\n{"a": "cut into 2" pieces", "b": [1, 2,],}\n```', {"a": 'cut into 2" pieces', "b": [1, 2]}),
    ('Here you go:\n[{"x": "line\nbreak"}]', [{"x": "line\nbreak"}]),
    ('{"s": "he said "hi", then left", "ok": true}', {"s": 'he said "hi", then left', "ok": True}),
    ('{"fine": "as is"}', {"fine": "as is"}),
])
def test_repairs(raw, expected):
    assert json_answer.parse(raw) == expected


def test_hopeless_answer_raises():
    with pytest.raises(json.JSONDecodeError):
        json_answer.parse('{"broken": ')


def test_translation_asks_again_after_unreadable_answer(monkeypatch):
    answers = iter(['{"title": "Geröstete Karotten', json.dumps(
        {"title": "Geröstete Karotten mit Harissa-Vinaigrette", "description": None,
         "steps": [{"title": None, "instruction": "Rösten."}], "ingredient_notes": {}})])
    prompts = []

    def fake(system_prompt, user, max_tokens=0, tools=False):
        prompts.append(system_prompt)
        return next(answers), TokenUsage(input_tokens=10, output_tokens=5)
    monkeypatch.setattr(tools_recipes.llm_provider, "complete_text", fake)
    recipe = {"name": "Roasted Carrots With Harissa Vinaigrette", "steps": [{"instruction": "Roast."}]}
    result, usage = tools_recipes.translate_recipe_text(recipe, "Deutsch")
    assert result["title"].startswith("Geröstete")
    assert len(prompts) == 2 and "not valid JSON" in prompts[1]
    assert usage.input_tokens == 20  # both calls counted
