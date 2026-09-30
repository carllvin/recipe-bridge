"""Moving recipes between Tandoor and Mealie: read as they are (no AI),
reviewed and imported like any other import, cookbooks carried over."""
import os

import httpx
from fastapi.testclient import TestClient

from app import jobs, main, mealie_client, migration, tandoor_client
from app.config import settings

from fake_mealie import FakeMealie
from fake_tandoor import FakeTandoor

PHOTO = b"\xff\xd8\xff\xe0photo"


def tandoor_source(monkeypatch):
    fake = FakeTandoor()
    handler = fake.handler

    def with_media(request):
        if request.url.path.startswith("/media/"):
            return httpx.Response(200, content=PHOTO, headers={"content-type": "image/jpeg"})
        return handler(request)
    fake.handler = with_media
    monkeypatch.setattr(tandoor_client, "get_client", fake.client)
    zwiebel, knoblauch = fake.add("food", {"name": "Zwiebel"}), fake.add("food", {"name": "Knoblauch"})
    g = fake.add("unit", {"name": "g"})
    suppe = fake.add("keyword", {"name": "Suppe"})
    soup = fake.add("recipe", {
        "name": "Zwiebelsuppe", "description": "Kräftig.", "servings": 4, "working_time": 15, "waiting_time": 40,
        "source_url": "https://example.com/zwiebelsuppe", "image": "http://tandoor/media/recipes/soup.jpg",
        "keywords": [{"id": suppe["id"]}],
        "steps": [
            {"name": "", "instruction": "Zwiebeln schneiden.", "ingredients": [
                {"food": None, "unit": None, "amount": 0, "note": "Für die Suppe", "is_header": True},
                {"food": {"id": zwiebel["id"]}, "unit": {"id": g["id"]}, "amount": 500, "note": "in Ringen"}]},
            {"name": "Kochen", "instruction": "40 Minuten köcheln.", "time": 40, "ingredients": [
                {"food": {"id": knoblauch["id"]}, "unit": None, "amount": 0, "no_amount": True, "note": ""}]},
        ]})
    fake.add("recipe", {"name": "Brot", "steps": [{"instruction": "Backen.", "ingredients": []}]})
    book = fake.add("recipe-book", {"name": "Omas Kochbuch"})
    fake.add("recipe-book-entry", {"book": book["id"], "recipe": soup["id"]})
    return fake


def mealie_target(monkeypatch):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client())
    return fake


def run(cookbook=None):
    job = jobs.create_job("move")
    os.makedirs(main._job_dir(job.id), exist_ok=True)
    main._run_migration(job.id, cookbook)
    return jobs.get_job(job.id)


def test_source_is_the_other_configured_manager(monkeypatch):
    monkeypatch.setattr(settings, "mealie_url", "")
    assert migration.source_name() is None  # Tandoor target, no Mealie
    monkeypatch.setattr(settings, "mealie_url", "https://m")
    monkeypatch.setattr(settings, "mealie_token", "t")
    assert migration.source_name() == "Mealie"
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    assert migration.source_name() == "Tandoor"


def test_tandoor_to_mealie(monkeypatch):
    tandoor_source(monkeypatch)
    mealie = mealie_target(monkeypatch)
    job = run()
    assert job.status == "ready", job.error
    soup = next(r for r in job.recipes if r.title == "Zwiebelsuppe")
    assert (soup.servings, soup.prep_time_minutes, soup.cook_time_minutes) == (4, 15, 40)
    assert soup.tags == ["Suppe"] and soup.cookbooks == ["Omas Kochbuch"]
    assert soup.source_url == "https://example.com/zwiebelsuppe"
    assert [(i.name, i.amount, i.unit, i.note, i.group, i.step_index) for i in soup.ingredients] == [
        ("Zwiebel", 500, "g", "in Ringen", "Für die Suppe", 0), ("Knoblauch", None, None, None, "Für die Suppe", 1)]
    assert [(s.title, s.instruction, s.time_minutes) for s in soup.steps] == [
        (None, "Zwiebeln schneiden.", None), ("Kochen", "40 Minuten köcheln.", 40)]
    assert soup.selected_image_id and job.images[soup.selected_image_id]["filename"].endswith(".jpg")

    data = TestClient(main.app).post(f"/api/jobs/{job.id}/import", json={}).json()
    assert all(r["status"] == "imported" for r in data["results"]), data
    moved = mealie.recipes["zwiebelsuppe"]
    assert [c["name"] for c in moved["recipeCategory"]] == ["Omas Kochbuch"]
    assert "zwiebelsuppe" in mealie.images and mealie.images["zwiebelsuppe"][1] != b""
    assert [c["name"] for c in mealie.recipes["brot"]["recipeCategory"]] == []


def test_one_cookbook_only_and_already_moved_ones_deselected(monkeypatch):
    tandoor_source(monkeypatch)
    mealie = mealie_target(monkeypatch)
    job = run("Omas Kochbuch")
    assert [r.title for r in job.recipes] == ["Zwiebelsuppe"] and job.cookbook_name == "Omas Kochbuch"
    TestClient(main.app).post(f"/api/jobs/{job.id}/import", json={})
    again = run()
    assert {r.title: r.selected for r in again.recipes} == {"Zwiebelsuppe": False, "Brot": True}
    assert len(mealie.recipes) == 1


def test_mealie_to_tandoor(monkeypatch, tandoor):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client())
    tag, cat = fake.add("tag", "Suppe"), fake.add("category", "Winter")
    food, unit = fake.add("food", "Zwiebel"), fake.add("unit", "g")
    fake.recipes["zwiebelsuppe"] = {
        "id": "abc", "slug": "zwiebelsuppe", "name": "Zwiebelsuppe", "description": "Kräftig.", "image": "1",
        "recipeServings": 4, "prepTime": "15 min", "performTime": "1 hour", "orgURL": "https://example.com/z",
        "tags": [tag], "recipeCategory": [cat],
        "recipeIngredient": [
            {"title": "Suppe", "quantity": 500, "unit": unit, "food": food, "note": "", "referenceId": "r1"},
            {"quantity": 0, "unit": None, "food": None, "note": "", "originalText": "1 Prise Salz", "referenceId": "r2"}],
        "recipeInstructions": [{"text": "Schneiden.", "ingredientReferences": [{"referenceId": "r1"}]},
                               {"title": "Kochen", "text": "Köcheln.", "ingredientReferences": [{"referenceId": "r2"}]}],
    }
    fake.images["zwiebelsuppe"] = ("webp", b"RIFFwebp")
    job = run()
    assert job.status == "ready", job.error
    soup = job.recipes[0]
    assert (soup.servings, soup.prep_time_minutes, soup.cook_time_minutes, soup.total_time_minutes) == (4, 15, 60, 75)
    assert [(i.name, i.amount, i.unit, i.group, i.step_index) for i in soup.ingredients] == [
        ("Zwiebel", 500, "g", "Suppe", 0), ("1 Prise Salz", None, None, "Suppe", 1)]
    assert soup.cookbooks == ["Winter"] and soup.source_url == "https://example.com/z"
    assert job.images[soup.selected_image_id]["filename"].endswith(".webp")

    data = TestClient(main.app).post(f"/api/jobs/{job.id}/import", json={}).json()
    assert data["results"][0]["status"] == "imported", data
    assert tandoor.names("recipe-book") == ["Winter"]
    entry = next(iter(tandoor.db["recipe-book-entry"].values()))
    assert entry["recipe"] == data["results"][0]["tandoor_recipe_id"]


def test_endpoints(monkeypatch):
    tandoor_source(monkeypatch)
    mealie_target(monkeypatch)
    started = []
    monkeypatch.setattr(main, "_run_migration", lambda job_id, cookbook: started.append(cookbook))
    api = TestClient(main.app)
    assert api.get("/api/config").json()["migration_source"] == "Tandoor"
    assert api.get("/api/migration/cookbooks").json()["names"] == ["Omas Kochbuch"]
    assert api.post("/api/migration", json={"cookbook": "Omas Kochbuch"}).json()["job_id"]
    assert started == ["Omas Kochbuch"]
