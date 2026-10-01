"""Maintain -> chat: the AI's plan from a fixed catalog, the recipes found
by the app itself, every change a suggestion (undoable with Tandoor)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import db_chat, llm_provider, main, mealie_client, tool_jobs
from app.config import settings
from fake_mealie import FakeMealie


class AI:
    def __init__(self, monkeypatch):
        self.actions, self.seen, self.reply = [], [], "Mache ich."
        monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
        monkeypatch.setattr(llm_provider, "complete_tool_text", self.ask)

    def ask(self, prompt, content, max_tokens=0, **kw):
        assert "{\"type\": \"add_tag\"" in prompt  # the catalog
        self.seen.append(json.loads(content))
        return json.dumps({"reply": self.reply, "actions": self.actions}), None


@pytest.fixture
def kitchen(tandoor):
    tandoor.add("food", {"id": 1, "name": "Lachs", "plural_name": ""})
    tandoor.add("food", {"id": 2, "name": "Räucherlachs", "plural_name": ""})
    tandoor.add("food", {"id": 3, "name": "Paprika rot", "plural_name": ""})
    tandoor.add("food", {"id": 4, "name": "rote Paprika", "plural_name": ""})
    tandoor.add("food", {"id": 5, "name": "Mehl", "plural_name": ""})
    tandoor.add("keyword", {"id": 20, "name": "Fisch"})
    tandoor.add("keyword", {"id": 21, "name": "Test"})
    tandoor.add("recipe", {"id": 10, "name": "Lachs mit Dill", "servings": 2, "image": None, "keywords": [{"id": 21}],
                           "steps": [{"instruction": "", "ingredients": [{"food": {"id": 1}, "unit": None, "amount": 200}]}]})
    tandoor.add("recipe", {"id": 11, "name": "Bagel", "servings": 4, "image": "x.jpg", "keywords": [{"id": 20}],
                           "steps": [{"instruction": "", "ingredients": [{"food": {"id": 2}, "unit": None, "amount": 50}]}]})
    tandoor.add("recipe", {"id": 12, "name": "Rührkuchen", "servings": 0, "image": None, "keywords": [],
                           "steps": [{"instruction": "", "ingredients": [{"food": {"id": 5}, "unit": None, "amount": 300}]}]})
    tandoor.add("recipe-book", {"id": 30, "name": "Winter"})
    return tandoor


def say(message, job_id=None):
    resp = TestClient(main.app).post("/api/tools/db-chat", json={"message": message, "job_id": job_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def apply(turn, n=0):
    return main._perform_suggestion_action(turn["job_id"], turn["suggestion_ids"][n], "apply")


def test_the_ai_gets_the_names_not_the_data(kitchen, monkeypatch):
    ai = AI(monkeypatch)
    say("Wie viele Rezepte habe ich?")
    seen = ai.seen[0]
    assert seen["recipe_count"] == 3 and seen["tags"] == ["Fisch", "Test"] and seen["cookbooks"] == ["Winter"]


def test_tag_every_recipe_with_salmon(kitchen, monkeypatch):
    ai = AI(monkeypatch)
    ai.actions = [{"type": "add_tag", "recipes": {"ingredient": ["Lachs"]}, "tag": "Fisch"}]
    turn = say("Alle Rezepte mit Lachs bekommen das Tag Fisch")
    (s,) = turn["suggestions"]
    # Lachs and Räucherlachs - the Bagel has the tag already
    assert s["summary"] == "recipes: add tag 'Fisch' (1)" and s["detail"]["recipe_ids"] == [10]
    applied = apply(turn)
    assert applied.status == "applied" and applied.undoable
    assert {k["id"] for k in kitchen.db["recipe"][10]["keywords"]} == {20, 21}
    assert main._perform_suggestion_action(turn["job_id"], s["id"], "undo").status == "undone"
    assert {k["id"] for k in kitchen.db["recipe"][10]["keywords"]} == {21}


def test_rename_onto_an_existing_name_becomes_a_merge(kitchen, monkeypatch):
    ai = AI(monkeypatch)
    ai.actions = [{"type": "rename", "entity": "food", "name": "Paprika rot", "new_name": "rote Paprika"}]
    turn = say("Benenne Paprika rot in rote Paprika um")
    (s,) = turn["suggestions"]
    assert s["kind"] == "merge" and s["detail"]["keep_id"] == 4 and s["detail"]["remove_ids"] == [3]
    assert apply(turn).status == "applied"
    assert "Paprika rot" not in kitchen.names("food")


def test_servings_cookbook_and_questions(kitchen, monkeypatch):
    ai = AI(monkeypatch)
    ai.actions = [{"type": "set_servings", "recipes": {"name": "kuchen"}, "servings": 12},
                  {"type": "add_to_cookbook", "recipes": {"missing": ["image"]}, "cookbook": "winter"},
                  {"type": "find", "recipes": {"missing": ["servings"]}}]
    turn = say("Kuchen auf 12 Portionen, alles ohne Foto ins Kochbuch Winter - und was hat keine Portionen?")
    servings, book = turn["suggestions"]
    assert servings["detail"]["recipe_ids"] == [12] and book["summary"] == "recipes: into cookbook 'Winter' (2)"
    assert turn["answers"] == [{"count": 1, "recipes": [{"id": 12, "name": "Rührkuchen"}]}]
    assert apply(turn, 0).status == "applied" and kitchen.db["recipe"][12]["servings"] == 12
    assert apply(turn, 1).status == "applied"
    assert sorted(e["recipe"] for e in kitchen.db["recipe-book-entry"].values()) == [10, 12]


def test_delete_a_tag_needs_confirmation(kitchen, monkeypatch):
    ai = AI(monkeypatch)
    ai.actions = [{"type": "delete_tag", "tag": "Test"}]
    turn = say("Lösch das Tag Test")
    (s,) = turn["suggestions"]
    assert s["detail"]["confirm"] and s["detail"]["flagged"] and s["preview"] == "• Lachs mit Dill"
    assert apply(turn).status == "applied"
    assert kitchen.names("keyword") == ["Fisch"] and kitchen.db["recipe"][10]["keywords"] == []


def test_what_cant_be_found_is_said(kitchen, monkeypatch):
    ai = AI(monkeypatch)
    ai.actions = [{"type": "rename", "entity": "food", "name": "Zitronengras", "new_name": "Zitronen-Gras"},
                  {"type": "fly_to_the_moon"},
                  {"type": "set_category", "foods": ["Mehl"], "category": "Backen"}]
    turn = say("…")
    assert turn["problems"] == ["no ingredient called 'Zitronengras'", "unknown action 'fly_to_the_moon'"]
    (s,) = turn["suggestions"]
    assert apply(turn).status == "applied"
    assert kitchen.db["food"][5]["supermarket_category"]["name"] == "Backen"


def test_the_conversation_continues_in_one_run(kitchen, monkeypatch):
    ai = AI(monkeypatch)
    first = say("Hallo")
    second = say("Und jetzt?", first["job_id"])
    assert second["job_id"] == first["job_id"]
    assert ai.seen[1]["history"] == [{"role": "user", "text": "Hallo"}, {"role": "assistant", "text": "Mache ich."}]


def test_mealie(monkeypatch):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    lachs, test_tag = fake.add("food", "Lachs"), fake.add("tag", "Test")
    fake.add_recipe("Lachs mit Dill", [(lachs, None)], tags=[test_tag], servings=2)
    fake.add_recipe("Brot", [], servings=1)
    ai = AI(monkeypatch)
    ai.actions = [{"type": "add_tag", "recipes": {"ingredient": ["Lachs"]}, "tag": "Fisch"},
                  {"type": "remove_tag", "recipes": {}, "tag": "Test"},
                  {"type": "set_servings", "recipes": {"name": "Brot"}, "servings": 8},
                  {"type": "add_to_cookbook", "recipes": {}, "cookbook": "Alltag"},
                  {"type": "set_category", "foods": ["Lachs"], "category": "Fisch"}]
    turn = say("…")
    assert turn["problems"] == ["supermarket categories exist only in Tandoor"]
    for n in range(4):
        assert apply(turn, n).status == "applied"
    soup = fake.recipes["lachs-mit-dill"]
    assert [t["name"] for t in soup["tags"]] == ["Fisch"]
    assert fake.recipes["brot"]["recipeServings"] == 8
    assert all([c["name"] for c in r["recipeCategory"]] == ["Alltag"] for r in fake.recipes.values())
