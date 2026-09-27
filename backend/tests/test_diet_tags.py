"""Diet / allergen tags in the 'suggest more tags' tool."""
import json

from app import tools_tags
from app.schemas import ToolJob


def recipe(rid, name, ingredients, keywords=()):
    return {"id": rid, "name": name, "keywords": [{"id": i, "name": k} for i, k in enumerate(keywords)],
            "steps": [{"ingredients": [{"food": {"name": n}} for n in ingredients]}]}


def test_diet_tags_follow_the_language_and_existing_spelling(monkeypatch):
    from app.config import settings
    assert tools_tags.diet_tags(["Vegetarisch", "schnell"])[:2] == ["Vegetarisch", "vegan"]
    monkeypatch.setattr(settings, "output_language", "English")
    assert tools_tags.diet_tags()[2] == "gluten-free"


def test_diet_tags_come_on_top_and_vegan_implies_vegetarian(monkeypatch):
    sent = {}

    def fake(job, prompt, payload, max_tokens):
        sent.update(payload)
        return [{"id": 1, "tags": ["herzhaft", "Vegan", "Tomate"], "diet": ["vegan", "glutenfrei", "keto"]},
                {"id": 2, "tags": [], "diet": ["glutenfrei", "vegetarisch"]}]
    monkeypatch.setattr(tools_tags, "_complete_json", fake)
    many = [f"Zutat {i}" for i in range(50)]
    recipes = [recipe(1, "Tomatensuppe", ["Tomate", "Olivenöl"], ["Suppe"]),
               recipe(2, "Großer Eintopf", many, ["vegetarisch"])]
    out = tools_tags.suggest_tags_suggestions(ToolJob(id="j", tool="tags_suggest_more"), recipes, ["herzhaft"])
    tags = {s.detail["recipe_id"]: s.detail["tags"] for s in out}
    # "Tomate" is an ingredient, "Vegan" moves to the diet tags, "keto" isn't one
    assert tags[1] == ["herzhaft", "vegetarisch", "vegan", "glutenfrei"]
    # incomplete ingredient list -> no "free of" tags; vegetarisch already there
    assert 2 not in tags
    assert sent["diet_tags"] == ["vegetarisch", "vegan", "glutenfrei", "laktosefrei", "nussfrei"]
    assert sent["recipes"][1]["ingredients_complete"] is False
    assert tools_tags.diet_checked() == {1, 2}


def test_failed_batches_are_not_marked_checked(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("API down")
    monkeypatch.setattr(tools_tags, "_complete_json", boom)
    tools_tags.suggest_tags_suggestions(ToolJob(id="j", tool="tags_suggest_more"), [recipe(5, "X", ["Reis"])], [])
    assert tools_tags.diet_checked() == set()
