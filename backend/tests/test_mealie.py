"""Imports into Mealie (RECIPE_MANAGER=mealie) against a fake Mealie API."""
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import import_matching, jobs, main, mealie_client, tools_new_recipes, tools_tags
from app.config import settings
from app.schemas import ExtractedRecipe, Ingredient, Step
from fake_mealie import FakeMealie


@pytest.fixture
def mealie(monkeypatch):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    fake.add("food", "Zwiebel", pluralName="Zwiebeln")
    fake.add("unit", "g")
    fake.add("tag", "Suppe")
    return fake


def soup_job(tmp_path):
    job = jobs.create_job("Omas Kochbuch.pdf")
    job.status = "ready"
    job.recipes = [ExtractedRecipe(
        id="r1", title="Zwiebelsuppe", description="Kräftig.", servings=4, prep_time_minutes=15, cook_time_minutes=40,
        source_page_start=1, source_page_end=1, source_url="https://example.com/zwiebelsuppe",
        tags=["Suppe", "Herbst"],
        ingredients=[Ingredient(name="Zwiebel", unit="g", amount=500, step_index=0),
                     Ingredient(name="Knoblauch", amount=2, note="gehackt", step_index=0),
                     Ingredient(name="Knoblauch", amount=1, step_index=1)],
        steps=[Step(instruction="Zwiebeln schneiden."), Step(instruction="Kochen.")],
    )]
    images_dir = os.path.join(main._job_dir(job.id), "images")
    os.makedirs(images_dir, exist_ok=True)
    Image.new("RGB", (40, 30), "orange").save(os.path.join(images_dir, "img.jpg"))
    job.images = {"i1": {"page": 1, "filename": "img.jpg"}}
    job.recipes[0].selected_image_id = "i1"
    jobs.save_job(job)
    return job


def test_import_into_mealie(mealie, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(tools_new_recipes, "start_after_import", lambda ids: started.append(ids))
    job = soup_job(tmp_path)
    api = TestClient(main.app)
    assert tools_new_recipes.status()["baseline_created"]
    data = api.post(f"/api/jobs/{job.id}/import", json={"cookbook_name": "Omas Kochbuch"}).json()
    assert [(r["status"], r["tandoor_recipe_id"]) for r in data["results"]] == [("imported", "zwiebelsuppe")]
    recipe = mealie.recipes["zwiebelsuppe"]
    ings = recipe["recipeIngredient"]
    assert [(i["food"]["name"], (i["unit"] or {}).get("name"), i["quantity"], i["note"]) for i in ings] == [
        ("Zwiebel", "g", 500, ""), ("Knoblauch", None, 2, "gehackt"), ("Knoblauch", None, 1, "")]
    assert len([f for f in mealie.db["food"].values() if f["name"] == "Knoblauch"]) == 1  # created once
    assert ings[0]["food"]["id"] == next(f["id"] for f in mealie.db["food"].values() if f["name"] == "Zwiebel")
    steps = recipe["recipeInstructions"]
    assert [len(s["ingredientReferences"]) for s in steps] == [2, 1]
    assert {t["name"] for t in recipe["tags"]} == {"Suppe", "Herbst"}
    assert recipe["recipeServings"] == 4 and recipe["prepTime"] == "15 min" and recipe["performTime"] == "40 min"
    assert recipe["orgURL"] == "https://example.com/zwiebelsuppe"
    assert [c["name"] for c in recipe["recipeCategory"]] == ["Omas Kochbuch"]
    assert mealie.images["zwiebelsuppe"][0] == "jpg"
    assert started == []  # no Tandoor post-processing
    assert tools_new_recipes.status()["new_count"] == 0  # reviewed on import - not "new"

    # undo the import
    assert api.post(f"/api/jobs/{job.id}/recipes/r1/undo-import").status_code == 200
    assert "zwiebelsuppe" not in mealie.recipes


def test_review_matches_against_mealie(mealie, monkeypatch):
    monkeypatch.setattr(tools_tags, "_complete_json", lambda *a, **kw: [])
    job = jobs.create_job("x")
    job.recipes = [ExtractedRecipe(id="r", title="t", source_page_start=1, source_page_end=1, tags=["suppe", "Neu"],
                                   ingredients=[Ingredient(name="Zwiebeln", unit="G"), Ingredient(name="Lauch")])]
    import_matching.match_job_ingredients(job)
    onion, leek = job.recipes[0].ingredients
    assert (onion.name, onion.tandoor_match, onion.unit, onion.unit_match) == ("Zwiebel", "matched", "g", "matched")
    assert leek.tandoor_match == "new"
    assert job.recipes[0].tags == ["Suppe", "Neu"] and job.recipes[0].tag_status == {"Suppe": "matched", "Neu": "new"}


def test_status_config_and_duplicates(mealie):
    api = TestClient(main.app)
    assert api.get("/api/tandoor/status").json() == {"connected": True}
    config = api.get("/api/config").json()
    assert config["recipe_manager"] == "Mealie" and config["recipe_url_base"] == "https://mealie.example/g/home/r/"
    assert config["manager_url"] == "https://mealie.example"
    mealie.recipes["kaese"] = {"name": "Käsekuchen", "slug": "kaese"}
    with mealie_client.get_client() as client:
        assert mealie_client.fetch_all_recipe_names(client) == ["Käsekuchen"]


def test_wrong_token_is_reported(mealie, monkeypatch):
    monkeypatch.setattr(settings, "mealie_token", "wrong")
    assert TestClient(main.app).get("/api/tandoor/status").json()["connected"] is False
