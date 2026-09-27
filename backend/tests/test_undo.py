"""Applying suggestions records an undo journal; undoing replays it against
the (fake) Tandoor - including merges, where a deleted food is recreated."""
import pytest

from app import main, tool_jobs, undo
from app.schemas import ToolSuggestion


def run_with(tool, suggestion):
    job = tool_jobs.create_tool_job(tool)
    job.status = "ready"
    job.suggestions = [suggestion]
    tool_jobs.save_tool_job(job)
    return job


@pytest.fixture
def kitchen(tandoor):
    tandoor.add("food", {"id": 1, "name": "Zwiebel", "plural_name": "Zwiebeln", "properties": [], "supermarket_category": None})
    tandoor.add("food", {"id": 2, "name": "Zwiebeln", "plural_name": "", "properties": [], "supermarket_category": None,
                         "description": "doppelt"})
    tandoor.add("unit", {"id": 5, "name": "g"})
    tandoor.add("unit", {"id": 6, "name": "EL"})
    tandoor.add("keyword", {"id": 8, "name": "Suppe"})
    tandoor.add("recipe", {"id": 10, "name": "Zwiebelsuppe", "keywords": [{"id": 8}], "steps": [
        {"instruction": "Schneiden", "ingredients": [{"food": {"id": 2}, "unit": {"id": 5}, "amount": 500},
                                                     {"food": {"id": 1}, "unit": None, "amount": 1}]}]})
    return tandoor


def ingredient_foods(tandoor):
    return [i["food"]["name"] for i in tandoor.db["recipe"][10]["steps"][0]["ingredients"]]


def test_merge_and_undo(kitchen):
    job = run_with("ingredients_review", ToolSuggestion(
        id="m1", kind="merge", summary="merge",
        detail={"type": "merge", "keep_id": 1, "keep_name": "Zwiebel", "remove_ids": [2]}))

    applied = main._perform_suggestion_action(job.id, "m1", "apply")
    assert applied.status == "applied" and applied.undoable
    assert kitchen.names("food") == ["Zwiebel"]
    assert ingredient_foods(kitchen) == ["Zwiebel", "Zwiebel"]

    undone = main._perform_suggestion_action(job.id, "m1", "undo")
    assert undone.status == "undone"
    assert kitchen.names("food") == ["Zwiebel", "Zwiebeln"]
    recreated = next(f for f in kitchen.db["food"].values() if f["name"] == "Zwiebeln")
    assert recreated["description"] == "doppelt" and recreated["id"] != 2  # new id in Tandoor
    assert ingredient_foods(kitchen) == ["Zwiebeln", "Zwiebel"]
    assert undo.load(job.id, "m1") is None  # journal is gone after a complete undo


def test_undo_removes_a_tag_it_created_unless_used_elsewhere(kitchen):
    kitchen.add("recipe", {"id": 11, "name": "Eintopf", "keywords": [], "steps": []})
    first = run_with("tags_season", ToolSuggestion(id="s1", kind="season", summary="", detail={"recipe_id": 10, "season": "Winter"}))
    main._perform_suggestion_action(first.id, "s1", "apply")
    assert kitchen.names("keyword") == ["Suppe", "Winter"]

    main._perform_suggestion_action(first.id, "s1", "undo")
    assert kitchen.names("keyword") == ["Suppe"]  # created by that change, used nowhere else

    main._perform_suggestion_action(first.id, "s1", "undo")  # a second undo is a no-op
    again = run_with("tags_season", ToolSuggestion(id="s2", kind="season", summary="", detail={"recipe_id": 10, "season": "Winter"}))
    other = run_with("tags_season", ToolSuggestion(id="s3", kind="season", summary="", detail={"recipe_id": 11, "season": "Winter"}))
    main._perform_suggestion_action(again.id, "s2", "apply")
    main._perform_suggestion_action(other.id, "s3", "apply")
    main._perform_suggestion_action(again.id, "s2", "undo")
    # "Winter" was created by s2 but recipe 11 uses it by now - it stays.
    assert "Winter" in kitchen.names("keyword")
    assert [k["name"] for k in kitchen.db["recipe"][10]["keywords"]] == ["Suppe"]


def test_created_objects_are_deleted_on_undo(kitchen):
    job = run_with("conversions", ToolSuggestion(id="c1", kind="conversion", summary="", detail={
        "food": {"id": 1, "name": "Zwiebel"}, "base_unit": {"id": 6, "name": "EL"},
        "converted_unit": {"id": 5, "name": "g"}, "converted_amount": 10}))
    main._perform_suggestion_action(job.id, "c1", "apply")
    assert len(kitchen.db["unit-conversion"]) == 1
    main._perform_suggestion_action(job.id, "c1", "undo")
    assert kitchen.db["unit-conversion"] == {}
