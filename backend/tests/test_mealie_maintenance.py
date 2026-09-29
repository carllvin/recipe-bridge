"""Maintenance tiles that also work with Mealie."""
import pytest
from fastapi.testclient import TestClient

from app import health, main, mealie_client, mealie_maintenance, tool_jobs, tools_tags, llm_provider
from app.config import settings
from fake_mealie import FakeMealie


@pytest.fixture
def mealie(monkeypatch):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    onion = fake.add("food", "Zwiebel")
    onions = fake.add("food", "Zwiebeln")
    fake.add("food", "Alte Zutat")
    g = fake.add("unit", "g")
    fake.add("unit", "Prise")
    soup = fake.add("tag", "Suppe")
    fake.add("tag", "Altes Tag")
    fake.add_recipe("Zwiebelsuppe", [(onion, g), (onion, None)], tags=[soup], servings=4, image="1")
    fake.add_recipe("Zwiebelkuchen", [(onions, g)])
    return fake


def run(tool, **meta):
    job = tool_jobs.create_tool_job(tool)
    job.meta.update({"target": "mealie", **meta})
    tool_jobs.save_tool_job(job)
    mealie_maintenance.run_scan(job.id)
    return tool_jobs.get_tool_job(job.id)


def test_overview(mealie):
    with mealie_client.get_client() as client:
        metrics, items = mealie_maintenance.compute(client)
    assert metrics["recipes_total"] == 2
    (pair,) = items["foods_duplicates"]  # order within the pair follows Mealie's random ids
    assert set(pair["name"].split(" ↔ ")) == {"Zwiebel", "Zwiebeln"}
    assert [i["name"] for i in items["foods_unused"]] == ["Alte Zutat"]
    assert [i["name"] for i in items["units_unused"]] == ["Prise"]
    assert [i["name"] for i in items["keywords_unused"]] == ["Altes Tag"]
    assert [i["recipe_id"] for i in items["recipes_without_servings"]] == ["zwiebelkuchen"]
    assert [i["recipe_id"] for i in items["recipes_without_image"]] == ["zwiebelkuchen"]


def test_merge_duplicates_keeps_the_more_used(mealie):
    job = run("ingredients_review", focus="duplicates")
    (s,) = job.suggestions
    assert (s.detail["keep_name"], s.detail["remove_name"]) == ("Zwiebel", "Zwiebeln")
    assert main._perform_suggestion_action(job.id, s.id, "apply").status == "applied"
    assert [f["name"] for f in mealie.db["food"].values()] == ["Zwiebel", "Alte Zutat"]
    assert mealie.recipes["zwiebelkuchen"]["recipeIngredient"][0]["food"]["name"] == "Zwiebel"


def test_delete_unused(mealie):
    job = run("unused_keywords")
    assert [s.detail["name"] for s in job.suggestions] == ["Altes Tag"]
    s = main._perform_suggestion_action(job.id, job.suggestions[0].id, "apply")
    assert s.status == "applied" and not s.undoable
    assert [t["name"] for t in mealie.db["tag"].values()] == ["Suppe"]


def test_servings(mealie, monkeypatch):
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(tools_tags, "_complete_json", lambda job, prompt, payload, max_tokens: [
        {"id": p["id"], "servings": 12} for p in payload])
    job = run("recipes_servings")
    assert [s.detail["recipe_id"] for s in job.suggestions] == ["zwiebelkuchen"]
    main._perform_suggestion_action(job.id, job.suggestions[0].id, "apply")
    assert mealie.recipes["zwiebelkuchen"]["recipeServings"] == 12


def test_start_routes_to_mealie_and_refuses_tandoor_tools(mealie):
    api = TestClient(main.app)
    assert api.post("/api/tools/tags/groups").status_code == 400
    job_id = api.post("/api/tools/unused/unit").json()["job_id"]
    job = tool_jobs.get_tool_job(job_id)
    assert job.meta["target"] == "mealie"
    assert api.get("/api/config").json()["health_metrics"] == mealie_maintenance.METRICS


def test_health_refresh_uses_mealie(mealie):
    health.compute_now()
    assert health.cached()["metrics"]["foods_unused"] == 1
    assert health.items("recipes_without_image")["items"][0]["recipe_id"] == "zwiebelkuchen"
