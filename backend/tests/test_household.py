"""The household profile: allergies never planned, dislikes and fixed
weekdays for the AI, persons as the servings of planned days."""
import datetime as dt

from fastapi.testclient import TestClient

from app import app_settings, cook_today, household, main, tool_jobs, tools_meal_plan
from test_cook_today import index  # noqa: F401 - fixture
from test_plan_chat import AI, ids, mealie  # noqa: F401 - fixture


def set_profile(**values):
    app_settings.update({"household": values})


def test_settings_are_validated():
    data = TestClient(main.app).put("/api/settings", json={"household": {
        "persons": "4", "avoid": " Nüsse, Garnelen ", "dislikes": "Pilze",
        "weekdays": {"4": "Pizza", "5": " ", "9": "x", "fri": "y"}}}).json()
    assert data["household"] == {"persons": 4, "avoid": "Nüsse, Garnelen", "dislikes": "Pilze", "weekdays": {"4": "Pizza"},
                                 "max_kcal": 0, "min_protein": 0, "goals": ""}
    assert household.for_ai() == {"persons": 4, "never": ["nüsse", "garnelen"], "dislikes": ["pilze"],
                                  "fixed_days": {"Friday": "Pizza"}, "max_kcal_per_serving": None,
                                  "min_protein_per_serving": None, "goals": ""}
    assert app_settings.get()["maintenance"]["hour"] == 3  # other sections untouched


def test_matching_ingredients_and_titles():
    foods = [["Walnüsse", ""], ["Mehl", ""]]
    assert household.avoided_in(foods, "Kuchen", {"avoid": "Nüsse", "dislikes": ""}) == ["nüsse"]
    assert household.avoided_in([["Mehl", ""]], "Nusskuchen", {"avoid": "Nuss", "dislikes": ""}) == ["nuss"]
    assert household.avoided_in(foods, "Kuchen", {"avoid": "Garnelen", "dislikes": ""}) == []


def test_cook_today_leaves_out_allergies_and_ranks_dislikes_last(index):  # noqa: F811
    set_profile(avoid="Sahne", dislikes="Feta")
    results = cook_today.suggest("Zucchini, Tomate, Eis")["results"]
    assert [r["name"] for r in results] == ["Tomatenreis", "Zucchini-Feta-Pfanne"]  # Eis has Sahne
    assert results[1]["disliked"] == ["feta"]


def test_weekly_plan_follows_the_profile(mealie, monkeypatch):  # noqa: F811
    set_profile(persons=2, avoid="Lachs", weekdays={"4": "Pizza"})
    next(r for r in mealie.recipes.values() if r["name"] == "Nudeln mit Feta")["recipeServings"] = 1
    ai = AI(monkeypatch)
    by_name = ids(mealie)
    ai.answer = lambda p: [{"date": d.split(" ")[0], "recipe_id": by_name[n], "reason": "ok"}
                           for d, n in zip(p["days"], ["Nudeln mit Feta", "Linsensuppe", "Rindergulasch"])]
    job = tool_jobs.create_tool_job("meal_plan")
    job.meta["params"] = {"start_date": (dt.date.today() + dt.timedelta(days=1)).isoformat(), "days": 3,
                          "meal_type": {"id": "dinner", "name": "Abendessen"}, "add_to_shopping": True}
    tool_jobs.save_tool_job(job)
    tools_meal_plan.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert job.status == "ready", job.error
    prompt, payload = ai.seen[0]
    assert "NEVER pick" in prompt and payload["household"]["fixed_days"] == {"Friday": "Pizza"}
    names = [line.split("|")[1] for line in payload["recipes"]]
    assert "Lachs mit Spinat" not in names and "Linsensuppe" in names

    # 2 persons, the recipe serves 1 -> twice onto the shopping list
    tools_meal_plan.apply_suggestion(job.id, job.suggestions[0].id)
    assert job.suggestions[0].detail["recipe"]["name"] == "Nudeln mit Feta"
    assert mealie.shopping_lists[0]["times"] == [2.0]


def test_tandoor_plan_entry_uses_the_persons(tandoor):
    set_profile(persons=6)
    recipe = tandoor.add("recipe", {"name": "Suppe", "servings": 4, "steps": []})
    with tandoor.client() as client:
        entry = tools_meal_plan.create_plan_entry(client, {"id": recipe["id"], "name": "Suppe"}, "2026-10-02",
                                                  {"id": 1, "name": "Abendessen"}, True)
    assert entry["servings"] == 6
    set_profile(persons=0)
    with tandoor.client() as client:
        entry = tools_meal_plan.create_plan_entry(client, {"id": recipe["id"], "name": "Suppe"}, "2026-10-03",
                                                  {"id": 1, "name": "Abendessen"}, False)
    assert entry["servings"] == 4
