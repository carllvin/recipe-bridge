"""The AI maintenance tools with Mealie: tags, translating, revising, new recipes, automation."""
import json

import pytest
from fastapi.testclient import TestClient

from app import health, llm_provider, main, maintenance, mealie_client, mealie_maintenance, mealie_tools, tool_jobs, tools_new_recipes
from app.config import settings
from fake_mealie import FakeMealie


class FakeAI:
    """Answers each prompt kind with what the test put in `answers` (a
    function of the parsed payload or a fixed value) and records the payloads."""

    MARKERS = {
        "tag_review": "clean up recipe tags in a database",
        "season": "clearly belong to one",
        "suggest": "suggest additional DESCRIPTIVE tags",
        "translate": "You translate cookbook recipe text",
        "restructure": "You improve the STRUCTURE",
        "normalize": "names in a home cook",
        "pick": "You match",
    }

    def __init__(self, monkeypatch):
        self.answers, self.seen = {}, {}
        monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
        monkeypatch.setattr(llm_provider, "complete_tool_text", self.ask)
        monkeypatch.setattr(llm_provider, "complete_text", self.ask)

    def ask(self, prompt, content, max_tokens=0, **kw):
        kind = next(k for k, marker in self.MARKERS.items() if marker in prompt)
        payload = json.loads(content)
        self.seen.setdefault(kind, []).append(payload)
        answer = self.answers.get(kind, [])
        return json.dumps(answer(payload) if callable(answer) else answer), None


@pytest.fixture
def mealie(monkeypatch):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    return fake


@pytest.fixture
def ai(monkeypatch):
    return FakeAI(monkeypatch)


def run(tool, **meta):
    job = tool_jobs.create_tool_job(tool)
    job.meta.update({"target": "mealie", **meta})
    tool_jobs.save_tool_job(job)
    mealie_maintenance.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert job.status == "ready", job.error
    return job


def apply_all(job):
    for s in job.suggestions:
        if s.status == "pending":
            result = mealie_maintenance.apply_suggestion(job.id, s.id)
            assert result.status == "applied", result.error
    return tool_jobs.get_tool_job(job.id)


def soup(fake, **extra):
    onion, stock, g = fake.add("food", "Zwiebel"), fake.add("food", "Brühe"), fake.add("unit", "g")
    recipe = fake.add_recipe("Onion soup", [(onion, g), (stock, None)], description="A classic.", **extra)
    recipe["recipeIngredient"][0]["note"] = "finely sliced"
    return recipe


def test_view_puts_ingredients_on_the_step_that_references_them(mealie):
    recipe = soup(mealie, recipeInstructions=[{"id": "s1", "title": "", "text": "Slice.", "ingredientReferences": []},
                                              {"id": "s2", "title": "", "text": "Cook.", "ingredientReferences": []}])
    recipe["recipeInstructions"][1]["ingredientReferences"] = [{"referenceId": recipe["recipeIngredient"][1]["referenceId"]}]
    v = mealie_tools.view(recipe)
    assert [[i["food"]["name"] for i in s["ingredients"]] for s in v["steps"]] == [["Zwiebel"], ["Brühe"]]
    assert v["id"] == "onion-soup" and v["steps"][0]["ingredients"][0]["note"] == "finely sliced"


def test_tag_cleanup_renames_and_merges(mealie, ai):
    cakes, kuchen, quick = mealie.add("tag", "cakes"), mealie.add("tag", "Kuchen"), mealie.add("tag", "quick")
    r = mealie.add_recipe("Apfelkuchen", tags=[cakes])

    def answer(payload):
        ids = {t["name"]: t["id"] for t in payload}
        return [{"type": "merge", "keep_id": ids["Kuchen"], "keep_name": "Kuchen", "remove_ids": [ids["cakes"]]},
                {"type": "rename", "id": ids["quick"], "new_name": "schnell"}]
    ai.answers["tag_review"] = answer
    job = run("tags_cleanup")
    assert all(isinstance(t["id"], int) for t in ai.seen["tag_review"][0])  # small numbers, not uuids
    assert sorted(s.kind for s in job.suggestions) == ["merge", "rename"]
    apply_all(job)
    assert sorted(t["name"] for t in mealie.db["tag"].values()) == ["Kuchen", "schnell"]
    assert [t["id"] for t in r["tags"]] == [kuchen["id"]]
    assert quick["id"] in mealie.db["tag"]


def test_season_and_more_tags(mealie, ai):
    mealie.add("tag", "Hauptgericht")
    soup(mealie)
    ai.answers["season"] = lambda p: [{"id": r["id"], "season": "Winter"} for r in p]
    ai.answers["suggest"] = lambda p: [{"id": r["id"], "tags": ["Hauptgericht", "französisch"], "diet": []} for r in p["recipes"]]
    job = run("tags_season")
    assert ai.seen["season"][0][0]["id"] == "onion-soup"
    apply_all(job)
    job = run("tags_suggest_more")
    assert "Zwiebel" in ai.seen["suggest"][0]["recipes"][0]["ingredients"]
    apply_all(job)
    tags = [t["name"] for t in mealie.recipes["onion-soup"]["tags"]]
    assert tags == ["Winter", "Hauptgericht", "französisch"]
    assert {t["name"] for t in mealie.db["tag"].values()} == {"Hauptgericht", "Winter", "französisch"}


def test_translate_recipe(mealie, ai, monkeypatch):
    soup(mealie, recipeInstructions=[{"id": "s1", "title": "", "text": "Slice the onions and cook them slowly for an hour.",
                                      "ingredientReferences": []}])
    ai.answers["translate"] = {"title": "Zwiebelsuppe", "description": "Ein Klassiker.",
                               "steps": [{"title": None, "instruction": "Zwiebeln schneiden und eine Stunde langsam garen."}],
                               "ingredient_notes": {"0.0": "fein geschnitten"}}
    job = run("recipes_translate")
    assert len(job.suggestions) == 1 and "Zwiebelsuppe" in job.suggestions[0].preview
    apply_all(job)
    r = mealie.recipes["onion-soup"]
    assert (r["name"], r["description"]) == ("Zwiebelsuppe", "Ein Klassiker.")
    assert r["recipeInstructions"][0]["text"].startswith("Zwiebeln") and r["recipeInstructions"][0]["id"] == "s1"
    assert r["recipeIngredient"][0]["note"] == "fein geschnitten"


def test_restructure_assigns_ingredients_and_fills_servings_and_times(mealie, ai):
    text = "Die Zwiebeln in Ringe schneiden. In Butter langsam goldbraun braten. Mit Brühe aufgießen. 30 Minuten köcheln lassen."
    soup(mealie, recipeInstructions=[{"id": "s1", "title": "", "text": text, "ingredientReferences": []}])
    ai.answers["restructure"] = lambda p: {
        "steps": [{"title": None, "instruction": "Die Zwiebeln in Ringe schneiden. In Butter langsam goldbraun braten.",
                   "ingredients": ["i0"]},
                  {"title": None, "instruction": "Mit Brühe aufgießen. 30 Minuten köcheln lassen.", "ingredients": ["i1"]}],
        "servings": 4, "working_time": 15, "waiting_time": 30}
    job = run("recipes_restructure")
    assert len(job.suggestions) == 1
    apply_all(job)
    r = mealie.recipes["onion-soup"]
    refs = [[x["referenceId"] for x in s["ingredientReferences"]] for s in r["recipeInstructions"]]
    assert refs == [[r["recipeIngredient"][0]["referenceId"]], [r["recipeIngredient"][1]["referenceId"]]]
    assert r["recipeInstructions"][0]["id"] == "s1" and len(r["recipeInstructions"]) == 2
    assert (r["recipeServings"], r["prepTime"], r["performTime"], r["totalTime"]) == (4, "15 min", "30 min", "45 min")


def test_health_tiles_and_tools_offered(mealie, isolated):
    soup(mealie)
    with mealie_client.get_client() as client:
        _metrics, items = mealie_maintenance.compute(client)
    assert items["recipes_without_season"] == [{"key": "onion-soup", "name": "Onion soup", "recipe_id": "onion-soup"}]
    assert [i["key"] for i in items["recipes_few_tags"]] == ["onion-soup"]
    config = TestClient(main.app).get("/api/config").json()
    assert "recipes_not_translated" in config["health_metrics"] and "tags_cleanup" in config["available_tools"]
    assert "tags_groups" not in config["available_tools"]
    assert TestClient(main.app).post("/api/tools/tags/groups").status_code == 400


def test_new_recipes_run(mealie, ai, isolated):
    onion = mealie.add("food", "Zwiebel")
    mealie.add("tag", "Suppe")
    mealie.add_recipe("Alte Suppe", [(onion, None)])
    assert not tools_new_recipes.set_baseline(True)["needs_choice"]
    onions = mealie.add("food", "onions")
    soups = mealie.add("tag", "soups")
    mealie.add_recipe("Onion soup", [(onions, None)], tags=[soups], description="A classic French soup with onions.",
                      recipeInstructions=[{"id": "s1", "title": "", "text": "Cook the onions slowly for an hour.",
                                           "ingredientReferences": []}])
    assert tools_new_recipes.status()["new_count"] == 1

    ai.answers["translate"] = lambda p: {"title": "Zwiebelsuppe", "description": "Eine klassische französische Suppe.",
                                         "steps": [{"title": None, "instruction": "Die Zwiebeln eine Stunde langsam garen."}],
                                         "ingredient_notes": {}}
    ai.answers["normalize"] = lambda p: [{"id": e["id"], "name": {"onions": "Zwiebel", "soups": "Suppe"}.get(e["name"], e["name"]),
                                          "alternatives": []} for e in p]
    ai.answers["season"] = lambda p: [{"id": r["id"], "season": "Herbst"} for r in p]
    ai.answers["suggest"] = lambda p: [{"id": r["id"], "tags": ["französisch"], "diet": []} for r in p["recipes"]]
    client = TestClient(main.app)
    job_id = client.post("/api/tools/new-recipes/process").json()["job_id"]
    import time
    for _ in range(100):
        job = tool_jobs.get_tool_job(job_id)
        if job.status != "scanning":
            break
        time.sleep(0.05)
    assert job.status == "ready", job.error
    kinds = {s.kind: s for s in job.suggestions}
    assert kinds["translate_recipe"].status == "applied"
    assert mealie.recipes["onion-soup"]["name"] == "Zwiebelsuppe"
    merges = [s for s in job.suggestions if s.kind == "merge"]
    assert sorted(s.detail["entity"] for s in merges) == ["food", "keyword"]
    for s in job.suggestions:
        if s.status == "pending":
            assert client.post(f"/api/tools/jobs/{job_id}/suggestions/{s.id}/apply").status_code == 200
    recipe = mealie.recipes["onion-soup"]
    assert recipe["recipeIngredient"][0]["food"]["id"] == onion["id"]
    assert sorted(t["name"] for t in recipe["tags"]) == ["Herbst", "Suppe", "französisch"]
    assert tool_jobs.get_tool_job(job_id).meta.get("marked")
    assert tools_new_recipes.status()["new_count"] == 0


def test_automatic_maintenance_runs_the_mealie_tools(mealie, ai, isolated, monkeypatch):
    soup(mealie)
    assert "recipes_without_season" in maintenance.available_metrics()
    assert "foods_without_nutrition" not in maintenance.available_metrics()
    from app import app_settings
    cfg = app_settings.get()
    cfg["maintenance"].update({"enabled": True, "metrics": ["recipes_without_season", "foods_without_nutrition"]})
    monkeypatch.setattr(app_settings, "get", lambda: cfg)
    monkeypatch.setattr(health, "compute_now", lambda: health._compute())
    ai.answers["season"] = lambda p: [{"id": r["id"], "season": "Winter"} for r in p]
    maintenance.run_once("manual")
    runs = tool_jobs.list_tool_jobs("tags_season")
    assert len(runs) == 1 and runs[0].meta["target"] == "mealie" and len(runs[0].suggestions) == 1
    assert maintenance.status()["last_result"]["started"] == ["tags_season"]
