"""Amounts in the steps written while reading (step_amounts): markers from
the extraction, kept through the review, templates in Tandoor, plain names
in Mealie."""
from app import ai_extractor, step_amounts, tandoor_client
from app.config import settings
from app.schemas import ExtractedRecipe, Ingredient, Step


def extracted():
    return {"title": "Pfannkuchen", "source_page_start": 1, "source_page_end": 1,
            "ingredients": [{"name": "Mehl", "amount": 250, "unit": "g", "step_index": 0},
                            {"name": "Milch", "amount": 500, "unit": "ml", "step_index": 0},
                            {"name": "Salz", "amount": None, "unit": None, "step_index": 0},
                            {"name": "Butter", "amount": 20, "unit": "g", "note": "kalt", "step_index": 1}],
            "steps": [{"instruction": "Das [[0]] mit der [[1]] und [[2]] verrühren."},
                      {"instruction": "Nach 3 Min. [[3]] in der Pfanne schmelzen, 250 g [[0]] nachgeben. [[9]]"}]}


def test_markers_from_the_extraction():
    item = step_amounts.from_extraction(extracted())
    assert [i["ref"] for i in item["ingredients"]] == ["i0", "i1", "i2", "i3"]
    assert item["steps"][0]["instruction"] == "[[i0:Mehl]] mit [[i1:Milch]] und Salz verrühren."
    # "3 Min." stays (not Butter's amount); a marker used twice and an unknown one become plain
    assert item["steps"][1]["instruction"] == "Nach 3 Min. [[i3:Butter]] in der Pfanne schmelzen, 250 g Mehl nachgeben."


def test_extraction_keeps_them_on_the_recipe():
    recipe = ai_extractor._dedupe_recipes([extracted()])[0]
    assert recipe.ingredients[3].ref == "i3"
    assert step_amounts.has_markers(recipe.steps[0].instruction)


def test_prompt_asks_for_markers_only_with_tandoor(monkeypatch):
    assert "[[n]]" in ai_extractor._build_system_prompt(False)
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    assert "[[n]]" not in ai_extractor._build_system_prompt(False)


def review_recipe():
    """After the review: Butter moved to step 1, Milch deleted, a new ingredient without ref."""
    r = ai_extractor._dedupe_recipes([extracted()])[0]
    r.ingredients = [i for i in r.ingredients if i.name != "Milch"]
    r.ingredients[2].step_index = 0  # Butter now in step 1, first-mentioned in step 2
    r.ingredients.append(Ingredient(name="Zucker", amount=1, unit="EL", step_index=0))
    return r


def test_tandoor_gets_templates(tandoor):
    rid = tandoor_client.create_recipe(tandoor.client(), review_recipe())
    steps = tandoor.db["recipe"][rid]["steps"]
    # step 1: Mehl (0), Salz (1), Butter (2), Zucker (3); the deleted Milch reads as its name
    assert steps[0]["instruction"] == "{{ ingredients[0] }} mit Milch und Salz verrühren."
    # Butter moved to step 1 - in step 2 it is its plain name
    assert steps[1]["instruction"].startswith("Nach 3 Min. Butter in der Pfanne schmelzen")


def test_tandoor_template_with_note(tandoor):
    r = ai_extractor._dedupe_recipes([extracted()])[0]
    rid = tandoor_client.create_recipe(tandoor.client(), r)
    steps = tandoor.db["recipe"][rid]["steps"]
    assert steps[0]["instruction"] == "{{ ingredients[0] }} mit {{ ingredients[1] }} und Salz verrühren."
    assert steps[1]["instruction"].startswith("Nach 3 Min. {{ ingredients[0] }} ({{ ingredients[0].note }}) in der Pfanne")


def test_mealie_gets_plain_names():
    assert step_amounts.plain("[[i0:Mehl]] mit [[i1:Milch]] verrühren.") == "Mehl mit Milch verrühren."


def test_after_import_no_second_amounts_call():
    """Tandoor has the templates already - the run after the import leaves the amounts alone."""
    from app import recipe_amounts
    recipe = {"steps": [{"instruction": "{{ ingredients[0] }} mit {{ ingredients[1] }} verrühren.",
                         "ingredients": [{"food": {"name": "Mehl"}, "amount": 250, "note": ""},
                                         {"food": {"name": "Milch"}, "amount": 500, "note": ""}]}]}
    assert recipe_amounts.skip_reason(recipe, after_import=True) == "templates already"
