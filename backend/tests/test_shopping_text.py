"""The weekly plan's shopping list to share: added up, scaled to the
household, grouped by aisle, staples and what's at home to check."""
from fastapi.testclient import TestClient

from app import app_settings, main, tool_jobs
from app.schemas import ToolSuggestion
from test_plan_chat import mealie  # noqa: F401 - fixture


def plan(recipes, at_home=""):
    job = tool_jobs.create_tool_job("meal_plan")
    job.status = "ready"
    job.meta["params"] = {"at_home": at_home, "meal_type": {"id": 1, "name": "Abendessen"}}
    job.suggestions = [ToolSuggestion(id=f"s{n}", kind="meal_plan", summary="", status=status,
                                      detail={"date": f"2026-10-0{n + 3}", "recipe": recipe})
                       for n, (recipe, status) in enumerate(recipes)]
    tool_jobs.save_tool_job(job)
    return job


def test_tandoor_list_adds_up_scales_and_groups(tandoor):
    gemuese = tandoor.add("supermarket-category", {"name": "Gemüse"})
    tomate = tandoor.add("food", {"name": "Tomate", "plural_name": "Tomaten", "supermarket_category": gemuese})
    sahne = tandoor.add("food", {"name": "Sahne"})
    salz = tandoor.add("food", {"name": "Salz"})
    feta = tandoor.add("food", {"name": "Feta"})
    ml, g = tandoor.add("unit", {"name": "ml"}), tandoor.add("unit", {"name": "g"})

    def ing(food, amount, unit=None, **extra):
        return {"food": {"id": food["id"]}, "unit": {"id": unit["id"]} if unit else None, "amount": amount, **extra}
    one = tandoor.add("recipe", {"name": "Tomatensuppe", "servings": 2, "steps": [{"ingredients": [
        ing(tomate, 3), ing(sahne, 100, ml), ing(salz, 0, no_amount=True)]}]})
    two = tandoor.add("recipe", {"name": "Pasta", "servings": 4, "steps": [{"ingredients": [
        ing(tomate, 2), ing(sahne, 200, ml), ing(feta, 200, g)]}]})
    skipped = tandoor.add("recipe", {"name": "Nicht", "servings": 1, "steps": [{"ingredients": [ing(feta, 999, g)]}]})
    for r in (one, two, skipped):  # Tandoor's detail carries the category on the food
        for i in r["steps"][0]["ingredients"]:
            i["food"]["supermarket_category"] = tandoor.db["food"][i["food"]["id"]].get("supermarket_category")
    app_settings.update({"household": {"persons": 4}})
    job = plan([({"id": one["id"], "name": "Tomatensuppe"}, "pending"), ({"id": two["id"], "name": "Pasta"}, "applied"),
                ({"id": skipped["id"], "name": "Nicht"}, "skipped")], at_home="Feta")

    data = TestClient(main.app).get(f"/api/tools/meal-plan/{job.id}/shopping-list").json()
    assert data["days"] == ["2026-10-03", "2026-10-04"] and data["persons"] == 4
    groups = {g["category"]: [(i["amount"], i["unit"], i["name"]) for i in g["items"]] for g in data["groups"]}
    # Tomatensuppe x2 (2 -> 4 persons), Pasta x1
    assert groups == {"Gemüse": [(8, None, "Tomaten")], None: [(400, "ml", "Sahne")]}
    assert [g["category"] for g in data["groups"]] == ["Gemüse", None]  # without a category last
    assert [(i["name"], i["amount"]) for i in data["check"]] == [("Feta", 200), ("Salz", None)]


def test_mealie_labels_and_unparsed_lines(mealie):  # noqa: F811
    label = {"id": "l1", "name": "Kühlregal"}
    feta = next(f for f in mealie.db["food"].values() if f["name"] == "Feta")
    recipe = next(r for r in mealie.recipes.values() if r["name"] == "Nudeln mit Feta")
    recipe["recipeServings"] = 2
    for row in recipe["recipeIngredient"]:
        if row["food"]["id"] == feta["id"]:
            row["food"]["label"] = label
            row["quantity"] = 150
    recipe["recipeIngredient"].append({"referenceId": "x", "quantity": 0, "food": None, "unit": None,
                                       "note": "1 Bund Basilikum"})
    job = plan([({"id": recipe["id"], "slug": recipe["slug"], "name": recipe["name"]}, "pending")])
    data = TestClient(main.app).get(f"/api/tools/meal-plan/{job.id}/shopping-list").json()
    groups = {g["category"]: [(i["amount"], i["name"]) for i in g["items"]] for g in data["groups"]}
    assert groups["Kühlregal"] == [(150, "Feta")]
    assert (None, "1 Bund Basilikum") in groups[None] and (1, "Nudeln") in groups[None]


def test_unknown_plan():
    assert TestClient(main.app).get("/api/tools/meal-plan/nope/shopping-list").status_code == 400
