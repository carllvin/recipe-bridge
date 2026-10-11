"""Import option "photos in the steps": a recipe's further photos go into
the step they show (recipe page: its HowToStep images; cookbook: one image AI
call with a contact sheet) or all into step 1 - and are uploaded with it
(Tandoor: file storage + the step's file, Mealie: assets in the step text)."""
import json
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import app_settings, image_steps, jobs, llm_provider, main, tools_new_recipes, url_processor
from app.schemas import ExtractedRecipe, Ingredient, Step
from test_mealie import mealie  # noqa: F401 - fixture


def recipe_job(pages=(1, 1)):
    job = jobs.create_job("Kochbuch.pdf")
    job.status = "ready"
    job.recipes = [ExtractedRecipe(
        id="r1", title="Brot", source_page_start=pages[0], source_page_end=pages[1],
        ingredients=[Ingredient(name="Mehl", unit="g", amount=500, step_index=0)],
        steps=[Step(instruction="Teig kneten."), Step(instruction="Formen."), Step(instruction="Backen.")])]
    images_dir = os.path.join(main._job_dir(job.id), "images")
    os.makedirs(images_dir, exist_ok=True)
    for iid, page, kind in [("main", 1, None), ("a", 1, None), ("b", 2, None), ("c", 2, None),
                            ("next", 3, None), ("scan", 1, "page")]:
        Image.new("RGB", (60, 40), "orange").save(os.path.join(images_dir, f"{iid}.jpg"))
        job.images[iid] = {"page": page, "filename": f"{iid}.jpg", **({"kind": kind} if kind else {})}
    job.recipes[0].selected_image_id = "main"
    jobs.save_job(job)
    return job, images_dir


def mode(name):
    app_settings.update({"import": {"step_images": name}})


def placed(recipe):
    return [s.image_ids for s in recipe.steps]


def test_extra_images_only_own_pages_without_main_or_scans(isolated):
    job, _ = recipe_job(pages=(1, 2))
    assert image_steps.extra_images(job, job.recipes[0]) == ["a", "b", "c"]


def test_off_and_all_into_step_one(isolated):
    job, images_dir = recipe_job(pages=(1, 2))
    image_steps.assign_all(job, images_dir)
    assert placed(job.recipes[0]) == [[], [], []]
    mode("first")
    image_steps.assign_all(job, images_dir)
    assert placed(job.recipes[0]) == [["a", "b", "c"], [], []]


def test_each_photo_into_its_step_by_the_ai(isolated, monkeypatch):
    job, images_dir = recipe_job(pages=(1, 2))
    seen = []

    def look(jpeg, prompt, max_tokens=0):
        seen.append(prompt)
        assert jpeg[:2] == b"\xff\xd8"  # one contact sheet
        return json.dumps({"photos": [{"n": 1, "step": 2}, {"n": 2, "step": None}, {"n": 3, "step": 9}]}), None
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(llm_provider, "transcribe_image", look)
    mode("steps")
    image_steps.assign_all(job, images_dir)
    # b: the finished dish (null), c: a step that doesn't exist -> left out
    assert placed(job.recipes[0]) == [[], ["a"], []]
    assert "3 numbered photos" in seen[0] and "2. Formen." in seen[0]


def test_ai_fails_then_step_one(isolated, monkeypatch):
    job, images_dir = recipe_job(pages=(1, 2))
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(llm_provider, "transcribe_image", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no vision")))
    mode("steps")
    image_steps.assign_all(job, images_dir)
    assert placed(job.recipes[0]) == [["a", "b", "c"], [], []]


def test_recipe_page_step_images_by_position(isolated):
    job, images_dir = recipe_job()
    # the page had 6 steps with photos at steps 1 and 5; the AI made 3 of them
    job.images = {"main": job.images["main"],
                  "s1": {"page": 1, "filename": "a.jpg", "step": 0, "of": 6},
                  "s5": {"page": 1, "filename": "b.jpg", "step": 4, "of": 6}}
    mode("steps")
    image_steps.assign_all(job, images_dir)
    assert placed(job.recipes[0]) == [["s1"], [], ["s5"]]


def test_howto_step_images_are_read():
    found, steps = url_processor._step_images([
        {"@type": "HowToStep", "text": "Kneten", "image": "https://x/1.jpg"},
        {"@type": "HowToSection", "name": "Backen", "itemListElement": [
            {"@type": "HowToStep", "text": "Formen"},
            {"@type": "HowToStep", "text": "Backen", "image": [{"@type": "ImageObject", "url": "https://x/3.jpg"}]}]}])
    assert found == [(0, "https://x/1.jpg"), (2, "https://x/3.jpg")] and steps == 3


def test_import_to_tandoor_with_step_photos(tandoor, monkeypatch):
    monkeypatch.setattr(tools_new_recipes, "start_after_import", lambda ids: None)
    job, _ = recipe_job(pages=(1, 2))
    job.recipes[0].steps[0].image_ids = ["a", "b"]
    job.recipes[0].steps[2].image_ids = ["c", "main"]  # the main photo isn't shown twice
    jobs.save_job(job)
    api = TestClient(main.app)
    data = api.post(f"/api/jobs/{job.id}/import", json={}).json()
    assert data["results"][0]["status"] == "imported", data
    recipe = tandoor.db["recipe"][data["results"][0]["tandoor_recipe_id"]]
    files = list(tandoor.db["user-file"].values())
    assert [f["name"] for f in files] == ["Brot – 1.1", "Brot – 1.2", "Brot – 3.1"]
    first, _, last = recipe["steps"]
    assert first["file"]["id"] == files[0]["id"]
    assert first["instruction"] == f"Teig kneten.\n\n![](/media/files/{files[1]['id']}.jpg)"
    assert last["file"]["id"] == files[2]["id"] and last["instruction"] == "Backen."

    # undo: the step photos go too
    assert api.post(f"/api/jobs/{job.id}/recipes/r1/undo-import").status_code == 200
    assert not tandoor.db["user-file"]


def test_import_to_mealie_with_step_photos(mealie, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tools_new_recipes, "start_after_import", lambda ids: None)
    job, _ = recipe_job()
    job.recipes[0].steps[1].image_ids = ["a"]
    jobs.save_job(job)
    data = TestClient(main.app).post(f"/api/jobs/{job.id}/import", json={}).json()
    slug = data["results"][0]["tandoor_recipe_id"]
    recipe = mealie.recipes[slug]
    assert mealie.assets[slug] == ["step1.jpg"] and recipe["assets"][0]["fileName"] == "step1.jpg"
    assert recipe["recipeInstructions"][1]["text"] == f"Formen.\n\n![](/api/media/recipes/{recipe['id']}/assets/step1.jpg)"
    assert recipe["recipeInstructions"][0]["text"] == "Teig kneten."


def test_review_keeps_moved_photos(isolated):
    job, _ = recipe_job()
    steps = [{"instruction": "Teig kneten.", "image_ids": []}, {"instruction": "Formen.", "image_ids": ["a"]},
             {"instruction": "Backen."}]
    data = TestClient(main.app).put(f"/api/jobs/{job.id}/recipes/r1", json={"steps": steps}).json()
    assert [s["image_ids"] for s in data["steps"]] == [[], ["a"], []]
