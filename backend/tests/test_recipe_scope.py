"""Recipe tools read only what the health tile listed plus what changed."""
import json
import os

from app import recipe_scope
from app.config import settings
from app.tools_recipe_details import lacks_servings


def setup(tandoor):
    for rid, servings in [(10, 1), (11, 4), (12, 4), (13, 1)]:
        tandoor.add("recipe", {"id": rid, "name": f"R{rid}", "servings": servings, "updated_at": "t1",
                               "keywords": [], "steps": []})


def detail_reads(tandoor):
    return sorted(int(p.split("/")[2]) for m, p in tandoor.requests if m == "GET" and p.count("/") == 3)


def test_without_overview_everything_is_read(tandoor):
    setup(tandoor)
    with tandoor.client() as client:
        found = recipe_scope.recipes_for(client, "recipes_without_servings", lacks_servings)
    assert [r["id"] for r in found] == [10, 13]
    assert detail_reads(tandoor) == [10, 11, 12, 13]


def test_tile_list_plus_new_and_changed(tandoor):
    setup(tandoor)
    with open(os.path.join(settings.data_dir, "health.json"), "w") as f:
        json.dump({"computed_at": 1, "items": {"recipes_without_servings": [{"key": "10"}, {"key": "13"}]},
                   "recipe_versions": {"10": "t1", "11": "t1", "12": "t1", "13": "t1"}}, f)
    tandoor.db["recipe"][12]["updated_at"] = "t2"
    tandoor.db["recipe"][12]["servings"] = 0  # edited since: now lacks servings
    tandoor.add("recipe", {"id": 14, "name": "Neu", "servings": 1, "updated_at": "t2", "keywords": [], "steps": []})
    with tandoor.client() as client:
        found = recipe_scope.recipes_for(client, "recipes_without_servings", lacks_servings, skip={"13"})
    assert [r["id"] for r in found] == [10, 12, 14]
    assert detail_reads(tandoor) == [10, 12, 14]  # 11 unchanged and fine, 13 ignored


def test_old_overview_without_versions_uses_times(tandoor):
    import time
    setup(tandoor)
    now = time.time()
    tandoor.db["recipe"][11]["updated_at"] = "2020-01-01T10:00:00+00:00"
    tandoor.db["recipe"][12]["updated_at"] = "2020-01-01T10:00:00Z"
    tandoor.db["recipe"][13]["updated_at"] = "2099-01-01T10:00:00+00:00"  # edited after the overview
    del tandoor.db["recipe"][10]["updated_at"]  # unknown -> read
    with open(os.path.join(settings.data_dir, "health.json"), "w") as f:
        json.dump({"computed_at": now, "items": {"recipes_without_servings": []}}, f)
    with tandoor.client() as client:
        found = recipe_scope.recipes_for(client, "recipes_without_servings", lacks_servings)
    assert [r["id"] for r in found] == [10, 13]
    assert detail_reads(tandoor) == [10, 13]


def test_translate_reports_recipes_it_skips(tandoor, monkeypatch):
    from app import ignored, tool_jobs, tools_recipes
    tandoor.add("recipe", {"id": 20, "name": "Pancakes", "description": "", "keywords": [],
                           "steps": [{"name": "", "instruction": "Mix and fry.", "ingredients": []}]})
    tandoor.add("recipe", {"id": 21, "name": "Waffles", "description": "", "keywords": [],
                           "steps": [{"name": "", "instruction": "Mix and bake.", "ingredients": []}]})
    monkeypatch.setattr(tools_recipes.llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(tools_recipes, "already_in_target_language", lambda r, code: False)

    def fake(recipe, language):
        if recipe["id"] == 21:
            raise ValueError("kaputt")
        return {"title": recipe["name"], "description": "", "ingredient_notes": {},
                "steps": [{"title": None, "instruction": s["instruction"]} for s in recipe["steps"]]}, None
    monkeypatch.setattr(tools_recipes, "translate_recipe_text", fake)
    job = tool_jobs.create_tool_job("recipes_translate")
    tools_recipes.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert job.suggestions == []
    assert [(s["name"], s["reason"]) for s in job.meta["skipped"]] == [("Pancakes", "unchanged"), ("Waffles", "failed")]
    assert "20" in ignored.keys("recipes_not_translated") and "21" not in ignored.keys("recipes_not_translated")


def test_language_check_is_stable():
    from app import tools_recipes
    recipe = {"name": "Chili sin carne", "description": "", "steps": [{"instruction": "Bohnen kochen, fertig."}]}
    answers = {tools_recipes.already_in_target_language(recipe, "de") for _ in range(30)}
    assert len(answers) == 1
