"""TRANSLATE_RECIPES: translate into OUTPUT_LANGUAGE (default) or keep every
recipe in the language it was written in."""
from fastapi.testclient import TestClient

import pytest

from app import (ai_extractor, app_settings, llm_provider, main, maintenance, recipe_restructure, tool_jobs,
                 tools_new_recipes, tools_recipes)
from app.config import settings


@pytest.fixture
def keep_original(monkeypatch):
    return lambda: monkeypatch.setattr(settings, "translate_recipes", False)


def test_the_setting_reaches_the_page(isolated, keep_original):
    assert TestClient(main.app).get("/api/config").json()["translate_recipes"] is True
    keep_original()
    assert TestClient(main.app).get("/api/config").json()["translate_recipes"] is False


def test_extraction_prompt_keeps_the_language(isolated, keep_original):
    assert "Translate naturally" in ai_extractor._build_system_prompt(False)
    keep_original()
    prompt = ai_extractor._build_system_prompt(False)
    assert "Do NOT translate" in prompt and "Translate naturally" not in prompt
    assert "the cookbook's own language" in prompt  # tags, season, "pcs"
    assert "keep it, don't translate" in app_settings.recipe_language()


def test_no_translation_tile_or_automation(isolated, keep_original):
    assert "recipes_not_translated" in maintenance.available_metrics()
    keep_original()
    assert "recipes_not_translated" not in maintenance.available_metrics()


def test_new_recipes_are_not_translated(tandoor, monkeypatch, keep_original):
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


def test_name_cleanup_and_tags_keep_the_language(isolated, keep_original):
    from app.tools_new_recipes import NORMALIZE_SYSTEM_PROMPT, PICK_SYSTEM_PROMPT, _prompt
    assert "translate it if it's in another language" in _prompt(NORMALIZE_SYSTEM_PROMPT, "food")
    keep_original()
    normalize = _prompt(NORMALIZE_SYSTEM_PROMPT, "food")
    assert "never translate it" in normalize and "Deutsch" not in normalize and "translate it if" not in normalize
    assert "Keep every name in its own language" in _prompt(PICK_SYSTEM_PROMPT, "food")
    assert "never translate a tag" in ai_extractor._build_system_prompt(False, existing_tags=["Kuchen"])
