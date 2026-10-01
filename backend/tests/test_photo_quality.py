"""Weak photos: found by measuring the pixels (no AI, measured once and
cached), improved by the AI only when the suggestion is applied."""
import io
import random

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app import health, image_gen, main, mealie_client, photo_quality, tool_jobs, tools_recipe_details
from app.config import settings
from fake_mealie import FakeMealie


def photo(kind="good", size=(900, 600)):
    random.seed(1)
    img = Image.new("RGB", size, (180, 140, 90))
    draw = ImageDraw.Draw(img)
    for _ in range(300):
        x, y, r = random.randrange(size[0]), random.randrange(size[1]), random.randrange(5, 60)
        draw.ellipse((x, y, x + r, y + r), fill=tuple(random.randrange(256) for _ in range(3)))
    img = {"good": img, "blurry": img.filter(ImageFilter.GaussianBlur(6)), "dark": img.point(lambda v: v * 0.2),
           "flat": img.point(lambda v: 110 + v * 0.15)}[kind]
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


@pytest.mark.parametrize("kind,expected", [("good", []), ("blurry", ["blurry"]), ("flat", ["flat"])])
def test_measure(kind, expected):
    assert photo_quality.measure(photo(kind))["problems"] == expected


def test_dark_and_small():
    assert "dark" in photo_quality.measure(photo("dark"))["problems"]
    assert photo_quality.measure(photo(size=(300, 200)))["problems"] == ["small"]


def test_each_photo_is_measured_once():
    fetched = []

    def fetch(address):
        fetched.append(address)
        return photo("blurry" if "b" in address else "good")
    assert photo_quality.problems_of({"1": "/a.jpg", "2": "/b.jpg"}, fetch) == {"2": ["blurry"]}
    assert photo_quality.problems_of({"1": "/a.jpg", "2": "/b.jpg"}, fetch) == {"2": ["blurry"]}
    assert fetched == ["/a.jpg", "/b.jpg"]


@pytest.fixture
def ai_images(monkeypatch):
    monkeypatch.setattr(image_gen, "is_configured", lambda: True)
    monkeypatch.setattr(image_gen, "enhance_image", lambda data: b"\x89PNG improved")


def test_tandoor_tile_scan_and_apply(tandoor, ai_images):
    uploads = []
    handler = tandoor.handler

    def with_photos(request):
        path = request.url.path
        if path.startswith("/media/"):
            return httpx.Response(200, content=photo("blurry" if "blur" in path else "good"))
        if path.endswith("/image/") and request.method == "PUT":
            uploads.append((path, b"improved" in request.content))
            return httpx.Response(200, json={})
        return handler(request)
    tandoor.handler = with_photos
    tandoor.add("recipe", {"id": 1, "name": "Suppe", "image": "http://tandoor/media/blur.jpg", "steps": []})
    tandoor.add("recipe", {"id": 2, "name": "Kuchen", "image": "http://tandoor/media/sharp.jpg", "steps": []})
    health.compute_now()
    assert [i["name"] for i in health.items("recipes_weak_image")["items"]] == ["Suppe (unscharf)"]

    job = tool_jobs.create_tool_job("recipes_photos")
    tool_jobs.save_tool_job(job)
    tools_recipe_details.run_photos_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert job.status == "ready" and [s.summary for s in job.suggestions] == ["improve the photo of 'Suppe' (unscharf)"]
    assert main._perform_suggestion_action(job.id, job.suggestions[0].id, "apply").status == "applied"
    assert uploads == [("/api/recipe/1/image/", True)]


def test_a_replaced_photo_is_not_overwritten(tandoor, ai_images):
    tandoor.add("recipe", {"id": 1, "name": "Suppe", "image": "http://tandoor/media/new.jpg", "steps": []})
    job = tool_jobs.create_tool_job("recipes_photos")
    job.suggestions = [tools_recipe_details.photo_suggestion(1, "Suppe", "http://tandoor/media/old.jpg", ["dark"])]
    tool_jobs.save_tool_job(job)
    s = main._perform_suggestion_action(job.id, job.suggestions[0].id, "apply")
    assert s.status == "error" and "replaced" in s.error


def test_mealie(monkeypatch, ai_images):
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    fake.add_recipe("Suppe", [], image="k1")
    fake.add_recipe("Kuchen", [], image="k2")
    fake.images["suppe"] = ("jpg", photo("dark"))
    fake.images["kuchen"] = ("jpg", photo("good"))
    job = tool_jobs.create_tool_job("recipes_photos")
    job.meta["target"] = "mealie"
    tool_jobs.save_tool_job(job)
    from app import mealie_maintenance
    mealie_maintenance.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert job.status == "ready", job.error
    assert [s.detail["recipe_id"] for s in job.suggestions] == ["suppe"]
    assert main._perform_suggestion_action(job.id, job.suggestions[0].id, "apply").status == "applied"
    assert b"improved" in fake.images["suppe"][1]
