"""Nutrition goals of the household: energy/protein per serving from the
recipe manager, recipes above the energy limit left out of the weekly
plan, the rest of the goals for the AI."""
import datetime as dt

from app import app_settings, cook_today, nutrition, tool_jobs, tools_meal_plan
from test_plan_chat import AI, ids, mealie  # noqa: F401 - fixture


def test_per_serving_tandoor_and_mealie():
    tandoor = {"servings": 4, "food_properties": {
        "1": {"name": "Kalorien", "total_value": 2400}, "2": {"name": "Proteine", "total_value": 120},
        "3": {"name": "Fett", "total_value": 80}}}
    assert nutrition.per_serving(tandoor) == {"kcal": 600, "protein": 30}
    assert nutrition.per_serving({"nutrition": {"calories": "450 kcal", "proteinContent": "32,5 g"}}) == {
        "kcal": 450, "protein": 32.5}
    assert nutrition.per_serving({"servings": 0, "food_properties": {"1": {"name": "Energy", "total_value": 9}}}) == {
        "kcal": None, "protein": None}
    assert nutrition.line({"kcal": 600, "protein": 30}) == "600 kcal, 30 g protein" and nutrition.line({}) == "-"


def test_weekly_plan_follows_the_goals(mealie, monkeypatch):  # noqa: F811
    by_slug = {r["name"]: r for r in mealie.recipes.values()}
    by_slug["Rindergulasch"]["nutrition"] = {"calories": "980 kcal", "proteinContent": "55 g"}
    by_slug["Linsensuppe"]["nutrition"] = {"calories": "420 kcal", "proteinContent": "24 g"}
    cook_today._rebuild()
    app_settings.update({"household": {"max_kcal": 700, "min_protein": 20, "goals": "2x Fisch pro Woche"}})
    ai = AI(monkeypatch)
    names = ids(mealie)
    ai.answer = lambda p: [{"date": d.split(" ")[0], "recipe_id": names[n], "reason": "ok"}
                           for d, n in zip(p["days"], ["Lachs mit Spinat", "Linsensuppe"])]
    job = tool_jobs.create_tool_job("meal_plan")
    job.meta["params"] = {"start_date": (dt.date.today() + dt.timedelta(days=1)).isoformat(), "days": 2,
                          "meal_type": {"id": "dinner", "name": "Abendessen"}}
    tool_jobs.save_tool_job(job)
    tools_meal_plan.run_scan(job.id)
    assert tool_jobs.get_tool_job(job.id).status == "ready"
    prompt, payload = ai.seen[0]
    assert "nutrition goals" in prompt
    assert payload["household"]["max_kcal_per_serving"] == 700 and payload["household"]["goals"] == "2x Fisch pro Woche"
    lines = {line.split("|")[1]: line.split("|") for line in payload["recipes"]}
    assert "Rindergulasch" not in lines                                  # 980 kcal > 700
    assert lines["Linsensuppe"][10] == "420 kcal, 24 g protein"
    assert lines["Nudeln mit Feta"][10] == "-"                           # unknown stays in
