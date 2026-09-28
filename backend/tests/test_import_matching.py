"""Ingredients, units and tags are matched in the import review - so an
import through the helper leaves nothing for the Review page."""
from app import import_matching, jobs, llm_provider, main, tool_jobs, tools_ingredients, tools_new_recipes, tools_tags
from app.schemas import ExtractedRecipe, Ingredient, ToolSuggestion


def kitchen(tandoor):
    tandoor.add("food", {"id": 1, "name": "Zwiebel", "plural_name": "Zwiebeln"})
    tandoor.add("unit", {"id": 2, "name": "EL"})
    tandoor.add("unit", {"id": 3, "name": "g"})
    tandoor.add("keyword", {"id": 4, "name": "Vegetarisch"})
    tandoor.add("keyword", {"id": 5, "name": "Suppen"})
    tandoor.add("keyword", {"id": 6, "name": "Ernährung", "numchild": 2})


def test_units_and_tags_are_matched(tandoor, monkeypatch):
    kitchen(tandoor)
    # the AI confirms merely similar names: "EL." -> "EL", "Suppe" -> "Suppen"
    monkeypatch.setattr(tools_tags, "_complete_json",
                        lambda job, prompt, payload, max_tokens: [{"id": p["id"], "match_name": p["candidates"][0]} for p in payload])
    recipe = ExtractedRecipe(id="r", title="Zwiebelsuppe", source_page_start=1, source_page_end=1,
                             tags=["vegetarisch", "Suppe", "Schnell", "Ernährung"],
                             ingredients=[Ingredient(name="Zwiebeln", unit="EL.", amount=2),
                                          Ingredient(name="Salz", unit="g", amount=5),
                                          Ingredient(name="Brühe", unit="Schuss")])
    job = jobs.create_job("x")
    job.recipes = [recipe]
    import_matching.match_job_ingredients(job)
    onion, salt, broth = recipe.ingredients
    assert (onion.name, onion.tandoor_match) == ("Zwiebel", "matched")
    assert (onion.unit, onion.unit_match, onion.original_unit) == ("EL", "matched", "EL.")
    assert (salt.unit, salt.unit_match) == ("g", "exists")
    assert broth.unit_match == "new"
    assert recipe.tags == ["Vegetarisch", "Suppen", "Schnell", "Ernährung"]
    assert recipe.tag_status == {"Vegetarisch": "matched", "Suppen": "matched", "Schnell": "new", "Ernährung": "new"}
    assert recipe.tag_original == {"Vegetarisch": "vegetarisch", "Suppen": "Suppe"}  # group tags aren't offered


def test_after_import_only_ingredient_data_is_filled_in(tandoor, monkeypatch):
    tandoor.add("food", {"id": 1, "name": "Zwiebel"})
    tandoor.add("recipe", {"id": 7, "name": "Zwiebelsuppe", "keywords": [],
                           "steps": [{"instruction": "Kochen.", "ingredients": [{"food": {"id": 1}, "unit": None, "amount": 1}]}]})
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(tools_new_recipes, "_new_recipe_ids", lambda client, job_id=None: [7])
    calls = []
    monkeypatch.setattr(tools_new_recipes, "_match_actions", lambda *a: calls.append("match") or [])
    monkeypatch.setattr(tools_tags, "season_suggestions", lambda *a: calls.append("season") or [])
    monkeypatch.setattr(tools_tags, "suggest_tags_suggestions", lambda *a: calls.append("tags") or [])
    monkeypatch.setattr(tools_ingredients, "enrich_suggestions", lambda job, targets, categories: [
        ToolSuggestion(id="e1", kind="enrich", summary="Zwiebel: plural", detail={"food_id": 1})])
    monkeypatch.setattr(tools_ingredients, "enrich_targets", lambda foods, categories, nutrition=True: foods)

    def fake_apply(job_id, suggestion_id):
        job = tool_jobs.get_tool_job(job_id)
        s = next(x for x in job.suggestions if x.id == suggestion_id)
        s.status = "applied"
        tool_jobs.save_tool_job(job)
        return s
    monkeypatch.setattr(tools_ingredients, "apply_enrich_suggestion", fake_apply)
    monkeypatch.setattr(tools_new_recipes.tools_conversions, "conversion_suggestions", lambda *a: [])

    job = tool_jobs.create_tool_job("new_recipes")
    job.meta.update({"auto": True, "trigger": "import"})
    tool_jobs.save_tool_job(job)
    tools_new_recipes.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert calls == []  # no matching, no tag suggestions
    assert [(s.id, s.status) for s in job.suggestions] == [("e1", "applied")]
    assert job.suggestions[0].applied_at  # shows under "recently applied"
    from fastapi.testclient import TestClient
    assert TestClient(main.app).get("/api/inbox").json()["items"] == []  # nothing left for Review
