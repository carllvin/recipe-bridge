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


def test_a_hanging_site_fails_fast_with_a_hint(monkeypatch, tmp_path):
    def hang(request):
        raise httpx.ReadTimeout("no answer", request=request)
    monkeypatch.setattr(url_processor, "_client", lambda: httpx.Client(transport=httpx.MockTransport(hang)))
    with pytest.raises(url_processor.UrlImportError, match="doesn't answer"):
        url_processor.process_url(URL, str(tmp_path))


def test_photos_stop_once_their_host_hangs(monkeypatch, tmp_path):
    recipe = {**RECIPE, "image": "https://img.example/main.jpg", "recipeInstructions": [
        {"@type": "HowToStep", "text": f"Step {n} of the dough.", "image": f"https://img.example/{n}.jpg"}
        for n in range(1, 6)]}
    page = f'<script type="application/ld+json">{json.dumps(recipe)}</script>'
    asked = []

    def handler(request):
        if request.url.host == "img.example":
            asked.append(str(request.url))
            raise httpx.ConnectTimeout("hangs", request=request)
        return httpx.Response(200, text=page)
    monkeypatch.setattr(url_processor, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    result = url_processor.process_url(URL, str(tmp_path))
    assert result["images"] == {} and asked == ["https://img.example/main.jpg"]  # not 6 waits


TEASER = '<article class="card"><h3>More pasta ideas</h3><p>Try our lasagne</p></article>'
GUIDE = ("<div class='content'><h1>How to make fresh pasta</h1><h2>Ingredients</h2><ul><li>400 g 00 flour</li>"
         "<li>4 large eggs</li></ul><h2>Method</h2><ol><li>Tip the flour onto a clean surface and make a well.</li>"
         "<li>Crack in the eggs and mix with a fork, then knead for 10 minutes until smooth.</li></ol></div>")


def from_browser(html, tmp_path):
    return url_processor.process_url(URL, str(tmp_path), html=html)["pages"][0]["text"]


def test_recipe_entry_without_ingredients_falls_back_to_the_page(tmp_path):
    """Reported: only title and address came over - the page's Recipe entry
    had nothing else; the method was only in the page."""
    stub = {"@type": "Recipe", "name": "How to make fresh pasta", "image": "https://x/p.jpg", "url": URL}
    text = from_browser(f'<script type="application/ld+json">{json.dumps(stub)}</script><body>{TEASER}{GUIDE}</body>', tmp_path)
    assert "400 g 00 flour" in text and "knead for 10 minutes" in text


def test_largest_text_block_not_the_first_article(tmp_path):
    text = from_browser(f"<body>{TEASER}<main>{GUIDE}</main></body>", tmp_path)
    assert "knead for 10 minutes" in text and "lasagne" not in text


def test_howto_guide_read_like_a_recipe(tmp_path):
    howto = {"@type": "HowTo", "name": "How to make fresh pasta", "supply": [{"@type": "HowToSupply", "name": "400 g flour"}],
             "step": [{"@type": "HowToStep", "text": "Make a well in the flour."},
                      {"@type": "HowToStep", "text": "Knead the dough for 10 minutes."}]}
    text = from_browser(f'<script type="application/ld+json">{json.dumps(howto)}</script>', tmp_path)
    assert "- 400 g flour" in text and "2. Knead the dough" in text


def test_recipe_without_method_gets_the_page_text(tmp_path):
    partial = {"@type": "Recipe", "name": "Pasta", "recipeIngredient": ["400 g flour"]}
    text = from_browser(f'<script type="application/ld+json">{json.dumps(partial)}</script><body>{GUIDE}</body>', tmp_path)
    assert "- 400 g flour" in text and "Page text:" in text and "knead for 10 minutes" in text


TESCO_RECIPE = {  # as on realfood.tesco.com: lower-case type, step photos, entities in the text
    "@context": "http://schema.org/", "@type": "recipe", "name": "How to make fresh pasta",
    "image": ["https://realfood.example/media/main-472x310.jpg"],
    "recipeIngredient": ["350g 00 flour", "3 medium eggs, plus 3 egg yolks"],
    "recipeInstructions": [
        {"@type": "HowToStep", "name": "Mixing eggs and flour", "text": "Tip the flour onto a clean work surface.",
         "image": "https://realfood.example/media/step-1.jpg"},
        {"@type": "HowToStep", "name": "Kneading the dough",
         "text": "Knead the dough for 10 mins. It&rsquo;ll become more pliable.",
         "image": "https://realfood.example/media/step-2.jpg"}]}
ASPNET = ('<body><form method="post" id="form"><header>Menu</header><section id="phMain">'
          '<h1>How to make fresh pasta</h1><ol><li><h2>Mixing eggs and flour</h2><p>Tip the flour onto a clean '
          'work surface and make a well in the centre.</p></li><li><h2>Kneading the dough</h2><p>Knead the dough '
          'for 10 mins until smooth and elastic.</p></li></ol></section><footer>Tesco</footer></form></body>')


def test_tesco_lower_case_recipe_with_step_photos(monkeypatch, tmp_path):
    """Reported: only title and address arrived - "@type": "recipe" was not
    recognised, and the page text was thrown away with its <form>."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "orange").save(buf, "JPEG")
    monkeypatch.setattr(url_processor, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=buf.getvalue()))))
    page = f'<script type="application/ld+json">{json.dumps(TESCO_RECIPE)}</script>{ASPNET}'
    result = url_processor.process_url(URL, str(tmp_path), html=page)
    text = result["pages"][0]["text"]
    assert "- 350g 00 flour" in text and "2. Knead the dough for 10 mins. It’ll become" in text
    steps = sorted(i["step"] for i in result["images"].values() if "step" in i)
    assert steps == [0, 1] and len(result["images"]) == 3  # main photo + two step photos


def test_page_text_inside_a_form_is_kept(tmp_path):
    text = from_browser(ASPNET, tmp_path)
    assert "make a well in the centre" in text and "Knead the dough for 10 mins" in text and "Menu" not in text
