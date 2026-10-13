"""A tag with the recipe's source: the site, the channel, the cookbook's author."""
import json

from bs4 import BeautifulSoup

from app import llm_provider, source_tag, url_processor
from app.config import settings
from app.schemas import ExtractedRecipe, TokenUsage


def test_names_become_tags():
    assert source_tag.slug("NYT Cooking") == "nytcooking"
    assert source_tag.slug("Délice d'Été") == "delicedete"
    assert source_tag.slug("Küchengötter") == "küchengötter"
    assert source_tag.from_author("Yotam Ottolenghi, Sami Tamimi") == "ottolenghi"
    assert source_tag.from_author("Yotam Ottolenghi and Ixta Belfrage") == "ottolenghi"
    assert source_tag.from_author("Adobe InDesign CS6 (Macintosh)") == ""
    assert source_tag.from_address("https://cooking.nytimes.com/recipes/1") == "nytimes"
    assert source_tag.from_address("https://www.bbc.co.uk/food/recipes/x") == "bbc"
    assert source_tag.from_address("https://www.chefkoch.de/rezepte/1") == "chefkoch"


def test_a_page_names_itself():
    soup = BeautifulSoup('<meta property="og:site_name" content="NYT Cooking">', "html.parser")
    assert source_tag.from_page(soup, "https://cooking.nytimes.com/r", None) == "nytcooking"
    bare = BeautifulSoup("<p>x</p>", "html.parser")
    assert source_tag.from_page(bare, "https://ottolenghi.co.uk/r", {"publisher": {"name": "Ottolenghi"}}) == "ottolenghi"
    assert source_tag.from_page(bare, "https://www.chefkoch.de/r", {}) == "chefkoch"


def test_web_import_carries_the_tag(tmp_path):
    recipe = {"@context": "https://schema.org", "@type": "Recipe", "name": "Mushroom bourguignon",
              "recipeIngredient": ["500 g mushrooms"], "recipeInstructions": "Cook the mushrooms slowly for an hour."}
    html = (f'<head><meta property="og:site_name" content="NYT Cooking">'
            f'<script type="application/ld+json">{json.dumps(recipe)}</script></head><body></body>')
    result = url_processor.process_url("https://cooking.nytimes.com/recipes/1", str(tmp_path), html=html)
    assert result["source_tag"] == "nytcooking"


def test_tag_added_once_and_first():
    r = ExtractedRecipe(id="a", title="x", tags=["Vegetarisch", "Ottolenghi"], source_page_start=1, source_page_end=1)
    s = ExtractedRecipe(id="b", title="y", tags=["Suppe"], source_page_start=1, source_page_end=1)
    source_tag.add([r, s], "ottolenghi")
    assert r.tags == ["Vegetarisch", "Ottolenghi"] and s.tags == ["ottolenghi", "Suppe"]


def test_cookbook_author_asked_once(monkeypatch):
    asked = []
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)

    def ai(system, content, max_tokens=0):
        asked.append(content)
        return '{"author": "Yotam Ottolenghi"}', TokenUsage(input_tokens=300, output_tokens=10)
    monkeypatch.setattr(llm_provider, "complete_tool_text", ai)
    tag, usage = source_tag.guess_author([{"page": 1, "text": "SIMPLE\nYotam Ottolenghi\nEbury Press"}])
    assert tag == "ottolenghi" and usage.input_tokens == 300 and len(asked) == 1


def test_switch(monkeypatch):
    monkeypatch.setattr(settings, "source_tag", False)
    assert not source_tag.enabled()
