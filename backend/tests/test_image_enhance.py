"""AI enhancement of a recipe's own photos - in the review and while importing."""
import io
import types

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import image_gen, jobs, main
from app.config import settings
from test_mealie import mealie, soup_job  # noqa: F401 - fixture and helper


def png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def enhancer(monkeypatch):
    calls = []

    def enhance(data):
        calls.append(data)
        return png("blue")
    monkeypatch.setattr(image_gen, "is_configured", lambda: True)
    monkeypatch.setattr(image_gen, "enhance_image", enhance)
    return calls


def test_enhance_in_review(mealie, enhancer, tmp_path):  # noqa: F811
    job = soup_job(tmp_path)
    job.recipes[0].candidate_image_ids = ["i1"]
    jobs.save_job(job)
    api = TestClient(main.app)
    res = api.post(f"/api/jobs/{job.id}/recipes/r1/enhance-image", json={"image_id": "i1"})
    assert res.status_code == 200, res.text
    data = res.json()
    new_id = data["image_id"]
    assert data["recipe"]["candidate_image_ids"] == ["i1", new_id] and data["recipe"]["selected_image_id"] == new_id
    assert data["image"]["ai"] == "enhanced" and data["image"]["enhanced_from"] == "i1"
    assert api.get(f"/api/jobs/{job.id}/images/{new_id}").status_code == 200

    # again: the improved copy is reused, not paid for twice; an AI image isn't improved again
    assert api.post(f"/api/jobs/{job.id}/recipes/r1/enhance-image", json={"image_id": "i1"}).json()["image_id"] == new_id
    assert api.post(f"/api/jobs/{job.id}/recipes/r1/enhance-image", json={"image_id": new_id}).json()["image_id"] == new_id
    assert len(enhancer) == 1


def test_enhance_needs_image_ai(mealie, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "image_gen_enabled", False)
    job = soup_job(tmp_path)
    job.recipes[0].candidate_image_ids = ["i1"]
    jobs.save_job(job)
    res = TestClient(main.app).post(f"/api/jobs/{job.id}/recipes/r1/enhance-image", json={})
    assert res.status_code == 400 and "IMAGE_GEN_ENABLED" in res.json()["detail"]


def test_enhance_while_importing(mealie, enhancer, tmp_path):  # noqa: F811
    job = soup_job(tmp_path)
    data = TestClient(main.app).post(f"/api/jobs/{job.id}/import", json={"enhance_photos": True}).json()
    assert data["results"][0]["status"] == "imported" and not data["results"][0]["error"]
    assert png("blue") in mealie.images["zwiebelsuppe"][1]  # the improved photo was uploaded
    assert jobs.get_job(job.id).recipes[0].selected_image_id != "i1"


def test_import_keeps_the_original_when_enhancing_fails(mealie, tmp_path, monkeypatch):  # noqa: F811
    def broken(data):
        raise RuntimeError("quota")
    monkeypatch.setattr(image_gen, "is_configured", lambda: True)
    monkeypatch.setattr(image_gen, "enhance_image", broken)
    job = soup_job(tmp_path)
    data = TestClient(main.app).post(f"/api/jobs/{job.id}/import", json={"enhance_photos": True}).json()
    result = data["results"][0]
    assert result["status"] == "imported" and "original was used" in result["error"]
    assert jobs.get_job(job.id).recipes[0].selected_image_id == "i1"
    assert "zwiebelsuppe" in mealie.images


def test_without_the_option_nothing_is_enhanced(mealie, enhancer, tmp_path):  # noqa: F811
    job = soup_job(tmp_path)
    TestClient(main.app).post(f"/api/jobs/{job.id}/import", json={})
    assert enhancer == []


def test_openai_edit_keeps_details_and_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "image_gen_enabled", True)
    monkeypatch.setattr(settings, "image_gen_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "k")
    seen = []

    class Images:
        def edit(self, **kw):
            seen.append(kw)
            if "input_fidelity" in kw:
                raise ValueError("Unknown parameter: input_fidelity")
            import base64
            return types.SimpleNamespace(data=[types.SimpleNamespace(b64_json=base64.b64encode(png("green")).decode())])

    class FakeOpenAI:
        def __init__(self, api_key):
            self.images = Images()
    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    out = image_gen.enhance_image(png("red"))
    assert out == png("green")
    assert seen[0]["input_fidelity"] == "high" and "input_fidelity" not in seen[1]
    assert "Keep the dish exactly as it is" in seen[0]["prompt"]
    assert seen[0]["image"][0] == "photo.png"
