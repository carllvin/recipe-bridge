"""Renaming "gemahlene Mandeln" to "Mandeln" keeps "gemahlen" as the note of
the recipe lines (Tandoor and Mealie)."""
import pytest

from app import main, mealie_client, prep_notes, tool_jobs, tools_ingredients
from app.config import settings
from app.schemas import ToolSuggestion
from fake_mealie import FakeMealie


@pytest.mark.parametrize("old,new,note", [
    ("gemahlene Mandeln", "Mandeln", "gemahlen"),
    ("gemahlene Mandeln", "Mandel", "gemahlen"),
    ("Gehackte Zwiebeln", "Zwiebel", "gehackt"),
    ("geriebener Parmesan", "Parmesan", "gerieben"),
    ("fein gehackte Petersilie", "Petersilie", "fein gehackt"),
    ("geröstete Pinienkerne", "Pinienkerne", "geröstet"),
    ("ground almonds", "almonds", "ground"),
    ("ground almonds", "Mandeln", None),  # a translation - nothing to keep
    ("Zwiebeln", "Zwiebel", None),        # only the plural
])
def test_the_note_from_the_two_names(old, new, note):
    assert prep_notes.prep_note(old, new) == note


def test_with_note():
    assert prep_notes.with_note("", "gemahlen") == "gemahlen"
    assert prep_notes.with_note("blanchiert", "gemahlen") == "gemahlen, blanchiert"
    assert prep_notes.with_note("fein gemahlen", "gemahlen") == "fein gemahlen"


def job_with(detail):
    job = tool_jobs.create_tool_job("ingredients_review")
    job.status = "ready"
    job.suggestions = [ToolSuggestion(id="s1", kind=detail["type"], summary="", detail=detail)]
    tool_jobs.save_tool_job(job)
    return job


@pytest.fixture
def almonds(tandoor):
    tandoor.add("food", {"id": 1, "name": "gemahlene Mandeln"})
    tandoor.add("food", {"id": 2, "name": "Mandeln"})
    tandoor.add("food", {"id": 3, "name": "Zucker"})
    tandoor.add("recipe", {"id": 10, "name": "Makronen", "steps": [{"instruction": "Mischen", "ingredients": [
        {"food": {"id": 1}, "unit": None, "amount": 200, "note": "blanchiert"},
        {"food": {"id": 3}, "unit": None, "amount": 100, "note": ""}]}]})
    return tandoor


def rows(tandoor):
    return [(i["food"]["name"], i["note"]) for i in tandoor.db["recipe"][10]["steps"][0]["ingredients"]]


def test_tandoor_rename_keeps_the_preparation(almonds):
    job = job_with({"type": "rename", "id": 1, "new_name": "Mandel"})
    assert main._perform_suggestion_action(job.id, "s1", "apply").status == "applied"
    # (the fake keeps a copy of the food's name in the recipe - real Tandoor links it)
    assert [note for _food, note in rows(almonds)] == ["gemahlen, blanchiert", ""]
    assert almonds.db["food"][1]["name"] == "Mandel"


def test_tandoor_merge_keeps_the_preparation(almonds):
    job = job_with({"type": "merge", "keep_id": 2, "keep_name": "Mandeln", "remove_ids": [1]})
    assert main._perform_suggestion_action(job.id, "s1", "apply").status == "applied"
    assert rows(almonds) == [("Mandeln", "gemahlen, blanchiert"), ("Zucker", "")]
    assert almonds.names("food") == ["Mandeln", "Zucker"]


def test_the_suggestion_says_so():
    by_id = {1: {"name": "gemahlene Mandeln"}, 2: {"name": "Mandeln"}}
    text = tools_ingredients._describe({"type": "merge", "keep_id": 2, "keep_name": "Mandeln", "remove_ids": [1]}, by_id)
    assert text.endswith("(recipe note: 'gemahlen')")


def test_mealie_rename_and_duplicate_merge(monkeypatch):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    ground, plain = fake.add("food", "gemahlene Mandeln"), fake.add("food", "Mandeln")
    fake.add_recipe("Makronen", [(ground, None)])
    fake.add_recipe("Kuchen", [(ground, None), (plain, None)])

    job = job_with({"entity": "food", "type": "merge", "keep_id": plain["id"], "keep_name": "Mandeln",
                    "remove_id": ground["id"], "remove_name": "gemahlene Mandeln"})
    job.meta["target"] = "mealie"
    tool_jobs.save_tool_job(job)
    assert main._perform_suggestion_action(job.id, "s1", "apply").status == "applied"
    notes = [(r["food"]["name"], r["note"]) for r in fake.recipes["kuchen"]["recipeIngredient"]]
    assert notes == [("Mandeln", "gemahlen"), ("Mandeln", "")]
    assert fake.recipes["makronen"]["recipeIngredient"][0]["note"] == "gemahlen"
