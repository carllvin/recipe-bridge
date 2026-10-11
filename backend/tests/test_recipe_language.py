"""Import option "recipe language": translate into OUTPUT_LANGUAGE (default)
or keep every recipe in the language it was written in."""
from fastapi.testclient import TestClient

from app import (ai_extractor, app_settings, llm_provider, main, maintenance, recipe_restructure, tool_jobs,
                 tools_new_recipes, tools_recipes)


def keep_original():
    app_settings.update({"import": {"translate": False}})


def test_default_translates_and_the_setting_is_stored(isolated):
    assert app_settings.get()["import"] == {"translate": True, "step_images": "off"}
    data = TestClient(main.app).put("/api/settings", json={"import": {"translate": False, "step_images": "nonsense"}}).json()
    assert data["import"] == {"translate": False, "step_images": "off"}
    assert TestClient(main.app).get("/api/config").json()["translate_recipes"] is False


def test_extraction_prompt_keeps_the_language(isolated):
    assert "Translate naturally" in ai_extractor._build_system_prompt(False)
    keep_original()
    prompt = ai_extractor._build_system_prompt(False)
    assert "Do NOT translate" in prompt and "Translate naturally" not in prompt
    assert "the cookbook's own language" in prompt  # tags, season, "pcs"
    assert "keep it, don't translate" in app_settings.recipe_language()


def test_no_translation_tile_or_automation(isolated):
    assert "recipes_not_translated" in maintenance.available_metrics()
    keep_original()
    assert "recipes_not_translated" not in maintenance.available_metrics()


def test_new_recipes_are_not_translated(tandoor, monkeypatch):
    tandoor.add("recipe", {"id": 7, "name": "Pancakes", "description": "", "keywords": [],
                           "steps": [{"name": "", "instruction": "Mix everything and fry.", "ingredients": []}]})
    calls = []
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(tools_new_recipes, "_new_recipe_ids", lambda client, job_id=None: [7])
    monkeypatch.setattr(tools_recipes, "already_in_target_language", lambda r, code: False)
    monkeypatch.setattr(recipe_restructure, "needs_restructure", lambda r: True)
    monkeypatch.setattr(tools_recipes, "translate_recipe_text", lambda r, language: calls.append("translate"))
    monkeypatch.setattr(recipe_restructure, "plan_suggestion", lambda job, r: calls.append("restructure"))
    monkeypatch.setattr(llm_provider, "complete_tool_text", lambda *a, **kw: ("[]", None))
    monkeypatch.setattr(llm_provider, "complete_text", lambda *a, **kw: ("[]", None))
    keep_original()
    job = tool_jobs.create_tool_job("new_recipes")
    tool_jobs.save_tool_job(job)
    tools_new_recipes.run_scan(job.id)
    assert calls == ["restructure"]
