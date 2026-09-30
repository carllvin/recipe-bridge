"""Rests across the week: ingredients that usually leave a rest (cream,
fresh herbs, feta ...) are shown to the AI, and each planned day says which
other days use them too."""
import datetime as dt

from app import cook_today, perishability, tool_jobs, tools_meal_plan
from test_plan_chat import AI, ids, mealie  # noqa: F401 - fixture


def test_what_leaves_a_rest():
    assert perishability.leftovers_of(["Schlagsahne", "Saure Sahne", "Glatte Petersilie", "Butter", "Fischsauce"]) == {
        "sahne": "Schlagsahne", "saure sahne": "Saure Sahne", "petersilie": "Glatte Petersilie"}
    # English and German names of the same thing match
    assert perishability.leftovers_of(["cream"]).keys() == perishability.leftovers_of(["Sahne"]).keys()


def test_plan_marks_days_sharing_a_rest(mealie, monkeypatch):  # noqa: F811
    feta = next(f for f in mealie.db["food"].values() if f["name"] == "Feta")
    mealie.add_recipe("Ofengemüse mit Feta", [(feta, None)], totalTime="35 min")
    cook_today._rebuild()
    ai = AI(monkeypatch)
    by_name = ids(mealie)
    ai.answer = lambda p: [{"date": d.split(" ")[0], "recipe_id": by_name[n], "reason": "ok"}
                           for d, n in zip(p["days"], ["Nudeln mit Feta", "Ofengemüse mit Feta", "Linsensuppe", "Rindergulasch",
                                                        "Lachs mit Spinat"])]
    start = dt.date.today() + dt.timedelta(days=1)
    job = tool_jobs.create_tool_job("meal_plan")
    job.meta["params"] = {"start_date": start.isoformat(), "days": 5, "meal_type": {"id": "dinner", "name": "Abendessen"}}
    tool_jobs.save_tool_job(job)
    tools_meal_plan.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert job.status == "ready", job.error

    prompt, payload = ai.seen[0]
    assert "use up rests" in prompt
    lines = {line.split("|")[1]: line.split("|") for line in payload["recipes"]}
    assert lines["Nudeln mit Feta"][9] == "Feta" and lines["Linsensuppe"][9] == "-"

    shared = {s.detail["recipe"]["name"]: s.detail["shared"] for s in job.suggestions}
    day = [(start + dt.timedelta(days=i)).isoformat() for i in range(5)]
    assert shared["Nudeln mit Feta"] == [{"name": "Feta", "days": [day[1]]}]
    assert shared["Ofengemüse mit Feta"] == [{"name": "Feta", "days": [day[0]]}]
    assert shared["Lachs mit Spinat"] == [] and shared["Linsensuppe"] == []
