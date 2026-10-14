"""Short links and Pinterest pins lead on to the recipe page."""
import json

import httpx

from app import url_processor

RECIPE_PAGE = """<html><head><title>Shakshuka</title>
<script type="application/ld+json">{"@type": "Recipe", "name": "Shakshuka",
 "recipeIngredient": ["4 Eier", "400 g Tomaten"], "recipeInstructions": "Alles schmoren, Eier hinein."}</script>
</head><body>Shakshuka</body></html>"""


def _pin(body_extra="", head_extra=""):
    return f"<html><head><title>Pin</title>{head_extra}</head><body>Ein Pin{body_extra}</body></html>"


def _serve(monkeypatch, pages: dict, redirects: dict | None = None):
    seen = []

    def handler(request):
        url = str(request.url)
        seen.append(url)
        if url in (redirects or {}):
            return httpx.Response(301, headers={"location": redirects[url]})
        if url in pages:
            return httpx.Response(200, html=pages[url])
        return httpx.Response(404)
    monkeypatch.setattr(url_processor, "_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True))
    return seen


def test_short_link_keeps_the_recipe_page_address(monkeypatch, tmp_path):
    _serve(monkeypatch, {"https://food.example/shakshuka": RECIPE_PAGE},
           {"https://bit.ly/abc": "https://food.example/shakshuka"})
    result = url_processor.process_url("https://bit.ly/abc", str(tmp_path))
    assert result["url"] == "https://food.example/shakshuka"
    assert "Shakshuka" in result["pages"][0]["text"]


def test_pinterest_pin_leads_to_the_recipe(monkeypatch, tmp_path):
    state = json.dumps({"pin": {"images": {"orig": {"url": "https://i.pinimg.com/x.jpg"}},
                                "link": "https://food.example/shakshuka"}})
    pin = _pin(f'<script id="__PWS_DATA__" type="application/json">{state}</script>')
    seen = _serve(monkeypatch, {"https://www.pinterest.de/pin/123/": pin,
                                "https://food.example/shakshuka": RECIPE_PAGE},
                  {"https://pin.it/xyz": "https://www.pinterest.de/pin/123/"})
    result = url_processor.process_url("https://pin.it/xyz", str(tmp_path))
    assert result["url"] == "https://food.example/shakshuka"
    assert result["metadata_title"] == "Shakshuka"
    assert "https://food.example/shakshuka" in seen


def test_pin_target_sources():
    assert url_processor.pin_target(_pin(head_extra='<meta property="og:see_also" content="https://a.example/r">')) \
        == "https://a.example/r"
    ld = '<script type="application/ld+json">{"@type": "SocialMediaPosting", ' \
         '"sharedContent": {"url": "https://b.example/r"}}</script>'
    assert url_processor.pin_target(_pin(head_extra=ld)) == "https://b.example/r"
    escaped = '<script>{"link":"https:\\/\\/c.example\\/r"}</script>'
    assert url_processor.pin_target(_pin(escaped)) == "https://c.example/r"
    # only Pinterest itself or the pin's picture: no target
    assert url_processor.pin_target(_pin('<script>{"link":"https://www.pinterest.com/x/",'
                                         '"tracked_link":"https://i.pinimg.com/1.jpg"}</script>')) is None


def test_pin_without_link_is_read_itself(monkeypatch, tmp_path):
    text = "Zutaten: 4 Eier, 400 g Tomaten. Alles schmoren und die Eier hineinschlagen, 10 Minuten garen."
    _serve(monkeypatch, {"https://www.pinterest.com/pin/9/": _pin(f"<p>{text}</p>")})
    result = url_processor.process_url("https://www.pinterest.com/pin/9/", str(tmp_path))
    assert result["url"] == "https://www.pinterest.com/pin/9/" and "Tomaten" in result["pages"][0]["text"]


def test_ordinary_link_is_not_resolved(monkeypatch, tmp_path):
    seen = _serve(monkeypatch, {"https://food.example/shakshuka": RECIPE_PAGE})
    result = url_processor.process_url("https://food.example/shakshuka", str(tmp_path))
    assert result["url"] == "https://food.example/shakshuka" and seen == ["https://food.example/shakshuka"]
