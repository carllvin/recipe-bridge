"""Duplicates by address: the same page imported again is flagged, whatever its title."""
from app import jobs, main, source_index, target
from app.schemas import ExtractedRecipe


def test_addresses_are_normalized():
    a = source_index.normalize("https://www.cooking.nytimes.com/recipes/1020738-mushroom-bourguignon/?utm_source=x#top")
    assert a == source_index.normalize("http://cooking.nytimes.com/recipes/1020738-mushroom-bourguignon")
    assert source_index.normalize("https://site.example/r?id=3") != source_index.normalize("https://site.example/r?id=4")


def test_same_page_flagged_despite_another_title(monkeypatch):
    source_index.add("https://cooking.nytimes.com/recipes/1", "Pilz-Bourguignon")
    client = target.client()
    monkeypatch.setattr(client, "fetch_all_recipe_names", lambda c: ["Pilz-Bourguignon", "Suppe"])

    class Ctx:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(client, "get_client", lambda: Ctx())
    job = jobs.create_job("x")
    job.recipes = [
        ExtractedRecipe(id="a", title="Mushroom bourguignon", source_url="https://www.cooking.nytimes.com/recipes/1/",
                        source_page_start=1, source_page_end=1),
        ExtractedRecipe(id="b", title="Sesame noodles", source_url="https://cooking.nytimes.com/recipes/2",
                        source_page_start=1, source_page_end=1)]
    main._mark_duplicates(job)
    assert (job.recipes[0].duplicate_match, job.recipes[0].duplicate_exact, job.recipes[0].selected) == \
        ("Pilz-Bourguignon", True, False)
    assert job.recipes[1].duplicate_match is None


def test_deleted_recipe_is_no_duplicate(monkeypatch):
    """The index says imported, but the recipe is gone from the collection."""
    source_index.add("https://site.example/r", "Weg")
    client = target.client()
    monkeypatch.setattr(client, "fetch_all_recipe_names", lambda c: ["Anderes"])

    class Ctx:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(client, "get_client", lambda: Ctx())
    job = jobs.create_job("x")
    job.recipes = [ExtractedRecipe(id="a", title="Neu", source_url="https://site.example/r",
                                   source_page_start=1, source_page_end=1)]
    main._mark_duplicates(job)
    assert job.recipes[0].duplicate_match is None
