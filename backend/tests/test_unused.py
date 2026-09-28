"""Tiles 'unused ingredients / units / tags' and deleting them (undoable)."""
import json
import os
import time

from app import health, main, tool_jobs, tools_unused
from app.config import settings


def kitchen(tandoor):
    for fid, name in [(1, "Zwiebel"), (2, "Zwiebeln alt"), (3, "Gewürze")]:
        tandoor.add("food", {"id": fid, "name": name, "numchild": 1 if fid == 3 else 0})
    for uid, name in [(10, "g"), (11, "Prise alt"), (12, "ml")]:
        tandoor.add("unit", {"id": uid, "name": name})
    tandoor.add("unit-conversion", {"id": 50, "base_unit": {"id": 12, "name": "ml"}, "converted_unit": {"id": 10, "name": "g"}})
    for kid, name in [(20, "Suppe"), (21, "Altes Tag"), (22, "Ernährung")]:
        tandoor.add("keyword", {"id": kid, "name": name, "numchild": 1 if kid == 22 else 0})
    tandoor.add("recipe", {"id": 100, "name": "Zwiebelsuppe", "keywords": [{"id": 20}],
                           "steps": [{"instruction": "", "ingredients": [{"food": {"id": 1}, "unit": {"id": 10}, "amount": 1}]}]})


def test_find_unused_keeps_groups_and_conversion_units(tandoor):
    kitchen(tandoor)
    with tandoor.client() as client:
        recipes = [client.get("/recipe/100/").json()]
    used = tools_unused.used_ids(recipes)
    foods = list(tandoor.db["food"].values())
    protected = tools_unused.protected_unit_ids(foods, list(tandoor.db["unit-conversion"].values()))
    assert [f["name"] for f in tools_unused.find_unused("food", foods, used["food"])] == ["Zwiebeln alt"]  # not the group
    assert [u["name"] for u in tools_unused.find_unused("unit", list(tandoor.db["unit"].values()), used["unit"], protected)] == ["Prise alt"]
    assert [k["name"] for k in tools_unused.find_unused("keyword", list(tandoor.db["keyword"].values()), used["keyword"])] == ["Altes Tag"]


def run_tool(tool):
    job = tool_jobs.create_tool_job(tool)
    tools_unused.run_scan(job.id)
    return tool_jobs.get_tool_job(job.id)


def test_scan_apply_and_undo(tandoor):
    kitchen(tandoor)
    job = run_tool("unused_keywords")
    assert [s.detail["name"] for s in job.suggestions] == ["Altes Tag"]
    s = main._perform_suggestion_action(job.id, job.suggestions[0].id, "apply")
    assert s.status == "applied" and s.undoable and "Altes Tag" not in tandoor.names("keyword")
    main._perform_suggestion_action(job.id, job.suggestions[0].id, "undo")
    assert "Altes Tag" in tandoor.names("keyword")


def test_scan_uses_the_overview_and_rechecks_changed_recipes(tandoor):
    kitchen(tandoor)
    for rid in tandoor.db["recipe"]:
        tandoor.db["recipe"][rid]["updated_at"] = "t1"
    with open(os.path.join(settings.data_dir, "health.json"), "w") as f:
        json.dump({"computed_at": time.time(), "items": {"foods_unused": [{"key": "2", "name": "Zwiebeln alt"}]},
                   "recipe_versions": {"100": "t1"}}, f)
    # a new recipe uses the "unused" food meanwhile
    tandoor.add("recipe", {"id": 101, "name": "Neu", "updated_at": "t2", "keywords": [],
                           "steps": [{"instruction": "", "ingredients": [{"food": {"id": 2}, "unit": None, "amount": 1}]}]})
    tandoor.requests.clear()
    job = run_tool("unused_foods")
    assert job.suggestions == []
    assert ("GET", "/recipe/100/") not in tandoor.requests  # unchanged recipe not read again


def test_health_lists_unused(tandoor):
    kitchen(tandoor)
    health.compute_now()
    items = health.items("keywords_unused")["items"]
    assert [i["name"] for i in items] == ["Altes Tag"]
    assert [i["name"] for i in health.items("units_unused")["items"]] == ["Prise alt"]
