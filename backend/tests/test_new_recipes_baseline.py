"""Before the first new-recipes run the user chooses whether the existing
recipes count as handled."""
from fastapi.testclient import TestClient

from app import main, tools_new_recipes


def recipe(tandoor, rid, name):
    return tandoor.add("recipe", {"id": rid, "name": name, "steps": [], "keywords": []})


def test_asks_first_instead_of_deciding(tandoor):
    recipe(tandoor, 1, "Alt")
    recipe(tandoor, 2, "Auch alt")
    api = TestClient(main.app)
    data = api.get("/api/tools/new-recipes/status").json()
    assert data["needs_choice"] and data["existing_count"] == 2 and data["new_count"] == 0
    assert api.get("/api/tools/new-recipes/status").json()["needs_choice"]  # nothing decided behind the user's back


def test_existing_count_as_done(tandoor):
    recipe(tandoor, 1, "Alt")
    data = TestClient(main.app).post("/api/tools/new-recipes/baseline", json={"existing_done": True}).json()
    assert not data["needs_choice"] and data["new_count"] == 0 and data["handled_count"] == 1
    recipe(tandoor, 2, "Neu")
    assert tools_new_recipes.status()["new_count"] == 1


def test_process_the_existing_ones_too_and_change_later(tandoor):
    recipe(tandoor, 1, "Alt")
    recipe(tandoor, 2, "Auch alt")
    api = TestClient(main.app)
    data = api.post("/api/tools/new-recipes/baseline", json={"existing_done": False}).json()
    assert data["new_count"] == 2 and data["handled_count"] == 0
    # changed the mind: now they count as done after all
    assert api.post("/api/tools/new-recipes/baseline", json={"existing_done": True}).json()["new_count"] == 0


def test_no_automatic_run_before_the_choice(tandoor, monkeypatch):
    from app import llm_provider
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    recipe(tandoor, 1, "Alt")
    assert tools_new_recipes.auto_run_once() is None
