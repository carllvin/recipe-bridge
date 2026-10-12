"""Sites with bot protection: a browser-like request, a second try with
other headers, a clear hint - and the bookmark button hands over the page
the browser already has, so the app doesn't have to load it."""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, url_processor

RECIPE = {"@context": "https://schema.org", "@type": "Recipe", "name": "Fresh pasta",
          "recipeIngredient": ["400 g flour", "4 eggs"],
          "recipeInstructions": [{"@type": "HowToStep", "text": "Knead the dough for ten minutes."}]}
PAGE = f'<html><head><script type="application/ld+json">{json.dumps(RECIPE)}</script></head><body></body></html>'
URL = "https://realfood.example/step-by-step/how-to-make-fresh-pasta.html"


def site(monkeypatch, answers):
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        return answers.pop(0) if answers else httpx.Response(404)
    monkeypatch.setattr(url_processor, "_client", lambda: httpx.Client(
        headers=url_processor.HEADERS, transport=httpx.MockTransport(handler)))
    return seen


def test_second_try_with_other_headers(monkeypatch, tmp_path):
    seen = site(monkeypatch, [httpx.Response(403), httpx.Response(200, text=PAGE)])
    result = url_processor.process_url(URL, str(tmp_path))
    assert "Knead the dough" in result["pages"][0]["text"]
    assert seen[0]["accept"].startswith("text/html") and "Firefox" in seen[1]["user-agent"]


def test_blocked_twice_gives_a_hint(monkeypatch, tmp_path):
    site(monkeypatch, [httpx.Response(403), httpx.Response(403)])
    with pytest.raises(url_processor.UrlImportError, match="bookmark button"):
        url_processor.process_url(URL, str(tmp_path))


def test_page_from_the_browser_is_not_loaded_again(isolated, monkeypatch):
    seen = site(monkeypatch, [])
    captured = {}

    def run(job_id, sources, doc_type, *a):
        with open(os.path.join(main._job_dir(job_id), "page.html"), encoding="utf-8") as f:
            captured["html"] = f.read()
        captured["result"] = url_processor.process_url(sources[0], str(isolated / "img"), html=captured["html"])
    monkeypatch.setattr(main, "_run_extraction", run)
    res = TestClient(main.app).post("/api/import-page", json={"url": URL, "html": PAGE})
    assert res.status_code == 200, res.text
    import time
    for _ in range(50):
        if "result" in captured:
            break
        time.sleep(0.02)
    assert "Knead the dough" in captured["result"]["pages"][0]["text"]
    assert seen == []  # the site itself was never asked


def test_page_too_large_or_no_address(isolated):
    api = TestClient(main.app)
    assert api.post("/api/import-page", json={"url": URL, "html": "x" * (url_processor.MAX_PAGE_BYTES + 1)}).status_code == 413
    assert api.post("/api/import-page", json={"url": "not a link", "html": PAGE}).status_code == 400
