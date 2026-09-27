"""Importing a .txt file with recipe links."""
from app import jobs, main, url_processor
from app.schemas import ExtractedRecipe, TokenUsage
from app.url_processor import UrlImportError


def test_links_are_found_deduplicated_and_cleaned():
    text = """My favourites:
    https://example.com/pumpkin-soup
    - https://example.org/risotto?id=3.
    see (https://example.com/pumpkin-soup) again, and www.no-scheme.com
    ftp://not-a-web-link"""
    assert url_processor.links_from_text(text) == ["https://example.com/pumpkin-soup", "https://example.org/risotto?id=3"]


def test_link_list_is_capped():
    text = "\n".join(f"https://example.com/{i}" for i in range(80))
    assert len(url_processor.links_from_text(text)) == url_processor.MAX_LINKS


def test_each_link_becomes_its_own_recipe(tmp_path, monkeypatch):
    list_file = tmp_path / "links.txt"
    list_file.write_text("https://a.example/soup\nhttps://broken.example/x\nhttps://b.example/cake\nhttps://c.example/blog\n")

    def fake_process_url(url, images_dir):
        if "broken" in url:
            raise UrlImportError("Could not load the page: 404")
        return {"pages": [{"page": 1, "text": f"Recipe from {url}"}], "images": {}, "page_count": 1}

    def fake_extract(pages, existing_tags=None, **kw):
        text = pages[0]["text"]
        if "blog" in text:
            return [], TokenUsage(input_tokens=10)
        title = "Soup" if "soup" in text else "Cake"
        return [ExtractedRecipe(id=title, title=title, source_page_start=7, source_page_end=7)], TokenUsage(input_tokens=100)

    monkeypatch.setattr(main, "process_url", fake_process_url)
    monkeypatch.setattr(main, "extract_recipes_from_pages", fake_extract)
    monkeypatch.setattr(main, "_fetch_existing_tags", lambda: [])
    monkeypatch.setattr(main, "_mark_duplicates", lambda job: None)
    monkeypatch.setattr(main.import_matching, "match_job_ingredients", lambda job: None)

    job = jobs.create_job("links.txt")
    main._run_extraction(job.id, [str(list_file)], "txt")

    job = jobs.get_job(job.id)
    assert job.status == "ready"
    assert [(r.title, r.source_url, r.source_page_start) for r in job.recipes] == [
        ("Soup", "https://a.example/soup", 1), ("Cake", "https://b.example/cake", 3)]
    assert job.notes == ["https://broken.example/x – Could not load the page: 404",
                         "https://c.example/blog – no recipe found"]
    assert job.cookbook_name is None
    assert job.token_usage.input_tokens == 210


def test_upload_types():
    assert main._classify_upload(["links.txt"]) == ("txt", "")
    assert main._classify_upload(["recipes.md"]) == ("text", "")
    assert main._classify_upload(["Omas Rezepte.docx"]) == ("docx", "")
    assert main._classify_upload(["links.txt", "photo.jpg"])[0] == ""
