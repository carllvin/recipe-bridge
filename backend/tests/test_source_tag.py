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


# ---------- the tile "recipes without source" ----------

def test_which_recipes_lack_their_source_tag():
    store = {"hosts": {"cooking.nytimes.com": "nytcooking", "blog.example": ""}, "books": {"Simple": "ottolenghi",
                                                                                         "Abendessen": ""}}
    nyt = "https://cooking.nytimes.com/recipes/1"
    assert source_tag.missing(nyt, [], ["Suppe"], store) == ("hosts", "cooking.nytimes.com")
    assert source_tag.missing(nyt, [], ["nytcooking"], store) is None
    assert source_tag.missing(nyt, [], ["nytimes"], store) is None  # the address' own name counts too
    assert source_tag.missing("https://www.chefkoch.de/r/1", [], [], store) == ("hosts", "chefkoch.de")
    assert source_tag.missing("https://blog.example/r", [], [], store) is None  # looked, nothing found
    assert source_tag.missing("", ["Simple"], ["Ottolenghi"], store) is None
    assert source_tag.missing("", ["Simple"], [], store) == ("books", "Simple")
    assert source_tag.missing("", ["Abendessen"], [], store) is None
    assert source_tag.missing("", ["Plenty"], [], store) == ("books", "Plenty")
    assert source_tag.missing("", [], [], store) is None  # no source at all


def test_tandoor_tile_tags_by_source(tandoor, monkeypatch):
    from app import main, migration, tool_jobs
    tandoor.add("keyword", {"id": 50, "name": "Suppe"})
    tandoor.add("recipe", {"id": 1, "name": "Mushroom bourguignon", "keywords": [{"id": 50}], "steps": [],
                           "source_url": "https://cooking.nytimes.com/recipes/1"})
    tandoor.add("recipe", {"id": 2, "name": "Sesame noodles", "keywords": [], "steps": [],
                           "source_url": "https://cooking.nytimes.com/recipes/2"})
    tandoor.add("recipe", {"id": 3, "name": "Shakshuka", "keywords": [], "steps": []})
    tandoor.add("recipe", {"id": 4, "name": "Omas Kuchen", "keywords": [], "steps": []})
    monkeypatch.setattr(migration, "_tandoor_books", lambda client: {3: ["Simple"]})
    monkeypatch.setattr(source_tag, "site_tag", lambda url: "nytcooking")
    monkeypatch.setattr(source_tag, "book_tag", lambda book, titles: ("ottolenghi", TokenUsage(input_tokens=80)))
    job = tool_jobs.create_tool_job("recipes_source")
    tool_jobs.save_tool_job(job)
    source_tag.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert job.status == "ready", job.error
    found = {s.detail["tag"]: sorted(s.detail["recipe_ids"]) for s in job.suggestions}
    assert found == {"nytcooking": [1, 2], "ottolenghi": [3]}
    for s in job.suggestions:
        assert main._TOOL_APPLY["recipes_source"](job.id, s.id).status == "applied"
    names = {rid: [k["name"] for k in tandoor.db["recipe"][rid]["keywords"]] for rid in (1, 2, 3, 4)}
    assert names == {1: ["Suppe", "nytcooking"], 2: ["nytcooking"], 3: ["ottolenghi"], 4: []}
    # remembered: the overview needs no request to know they're done
    store = source_tag.known()
    assert store["hosts"]["cooking.nytimes.com"] == "nytcooking" and store["books"]["Simple"] == "ottolenghi"
    assert source_tag.missing("https://cooking.nytimes.com/recipes/2", [], names[2], store) is None


def test_mealie_tile_tags_by_source(monkeypatch):
    from fake_mealie import FakeMealie
    from app import mealie_client
    from test_mealie_tools import apply_all, run
    fake = FakeMealie()
    monkeypatch.setattr(settings, "recipe_manager", "mealie")
    monkeypatch.setattr(settings, "mealie_url", "https://mealie.example")
    monkeypatch.setattr(settings, "mealie_token", "secret")
    monkeypatch.setattr(mealie_client, "get_client", lambda: fake.client(settings.mealie_token))
    r = fake.add_recipe("Noodles", [])
    r["orgURL"] = "https://cooking.nytimes.com/recipes/2"
    monkeypatch.setattr(source_tag, "site_tag", lambda url: "nytcooking")
    job = run("recipes_source")
    assert [s.detail["tag"] for s in job.suggestions] == ["nytcooking"]
    apply_all(job)
    assert [t["name"] for t in fake.recipes["noodles"]["tags"]] == ["nytcooking"]


def test_import_remembers_its_source(tandoor, monkeypatch):
    """Imported recipes carry the tag already - their site and cookbook are
    remembered, so they don't show up in the tile."""
    from fastapi.testclient import TestClient
    from app import jobs, main
    job = jobs.create_job("Simple.epub")
    job.status, job.source_tag, job.cookbook_name = "ready", "ottolenghi", "Simple"
    job.recipes = [ExtractedRecipe(id="r1", title="Shakshuka", tags=["ottolenghi"], selected=True,
                                   source_page_start=1, source_page_end=1)]
    jobs.save_job(job)
    monkeypatch.setattr(main.tools_new_recipes, "start_after_import", lambda ids: None)
    TestClient(main.app).post(f"/api/jobs/{job.id}/import", json={"cookbook_name": "Simple"})
    assert source_tag.known()["books"]["Simple"] == "ottolenghi"
