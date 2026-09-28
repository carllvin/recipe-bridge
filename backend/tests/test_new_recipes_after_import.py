"""After an import through this app the post-processing doesn't translate
or restructure again - the AI already did that while reading the recipe."""
import pytest

from app import llm_provider, recipe_restructure, tool_jobs, tools_new_recipes, tools_recipes


@pytest.mark.parametrize("trigger, translated", [("import", False), (None, True)])
def test_translation_only_for_recipes_added_in_tandoor(tandoor, monkeypatch, trigger, translated):
    tandoor.add("recipe", {"id": 7, "name": "Pancakes", "description": "", "keywords": [],
                           "steps": [{"name": "", "instruction": "Mix everything and fry.", "ingredients": []}]})
    calls = []
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(tools_new_recipes, "_new_recipe_ids", lambda client, job_id=None: [7])
    monkeypatch.setattr(tools_recipes, "already_in_target_language", lambda r, code: False)
    monkeypatch.setattr(recipe_restructure, "needs_restructure", lambda r: True)

    def fake_translate(recipe, language):
        calls.append("translate")
        raise RuntimeError("stop here")
    monkeypatch.setattr(tools_recipes, "translate_recipe_text", fake_translate)
    monkeypatch.setattr(recipe_restructure, "plan_suggestion", lambda job, r: calls.append("restructure"))
    # the later steps (matching, enrichment) are not what this test is about
    monkeypatch.setattr(llm_provider, "complete_tool_text", lambda *a, **kw: ("[]", None))
    monkeypatch.setattr(llm_provider, "complete_text", lambda *a, **kw: ("[]", None))

    job = tool_jobs.create_tool_job("new_recipes")
    if trigger:
        job.meta["trigger"] = trigger
    tool_jobs.save_tool_job(job)
    tools_new_recipes.run_scan(job.id)
    assert (calls == ["translate", "restructure"]) if translated else (calls == [])
