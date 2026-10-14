"""A cookbook's recipes are picked before the AI reads it - only their pages are sent."""
import time

import fitz
from fastapi.testclient import TestClient

from app import main, recipe_picker
from app.schemas import ExtractedRecipe, TokenUsage

RECIPE = "{title}\n\n200 g Mehl\n2 Eier\n1 TL Salz\n500 ml Milch\n\n1. Alles verrühren.\n2. Backen."
STORY = "Als ich ein Kind war, kochte meine Großmutter jeden Sonntag. " * 20


def cookbook(path, bookmarks=True):
    """24 pages: foreword (1-3), soups (4-13, a recipe every other page), cakes (14-23), index (24)."""
    doc = fitz.open()
    titles = {}
    for n in range(1, 25):
        page = doc.new_page()
        if 4 <= n <= 23 and n % 2 == 0:
            titles[n] = f"Rezept {n}"
            text = RECIPE.format(title=titles[n])
        else:
            text = STORY
        page.insert_textbox(fitz.Rect(40, 40, 560, 800), text, fontsize=10)
    if bookmarks:
        toc = [[1, "Vorwort", 1], [1, "Suppen", 4]]
        toc += [[2, titles[n], n] for n in titles if n < 14]
        toc += [[1, "Kuchen", 14]] + [[2, titles[n], n] for n in titles if n >= 14] + [[1, "Register", 24]]
        doc.set_toc(toc)
    doc.save(path)
    return titles


def test_bookmarks_become_the_list(tmp_path):
    from app.pdf_processor import process_pdf
    cookbook(tmp_path / "b.pdf")
    result = process_pdf(str(tmp_path / "b.pdf"), str(tmp_path / "img"))
    found, source = recipe_picker.entries(result)
    assert source == "bookmarks"
    assert [e["title"] for e in found][:3] == ["Vorwort", "Rezept 4", "Rezept 6"]
    assert found[1]["group"] == "Suppen" and (found[1]["start"], found[1]["end"]) == (4, 5)
    assert "Suppen" not in [e["title"] for e in found]  # a chapter is a heading, not an entry
    assert recipe_picker.worth_asking("pdf", result, found)


def test_without_bookmarks_recipe_pages_are_found(tmp_path):
    from app.pdf_processor import process_pdf
    titles = cookbook(tmp_path / "n.pdf", bookmarks=False)
    result = process_pdf(str(tmp_path / "n.pdf"), str(tmp_path / "img"))
    found, source = recipe_picker.entries(result)
    assert source == "pages"
    assert [e["title"] for e in found] == list(titles.values())
    assert all(e["end"] == e["start"] + 1 for e in found)  # the method may run onto the next page


def test_picked_pages_include_the_next_page():
    found = [{"id": "e0", "start": 4, "end": 5}, {"id": "e1", "start": 10, "end": 10}]
    pages = [{"page": n, "text": "x"} for n in range(1, 21)]
    picked, starts = recipe_picker.picked_pages(found, {"e1"}, pages)
    assert [p["page"] for p in picked] == [10, 11] and starts == {10}


def test_only_the_picked_pages_go_to_the_ai(tmp_path, monkeypatch):
    cookbook(tmp_path / "c.pdf")
    seen = {}

    def extract(pages, **kw):
        seen["pages"] = [p["page"] for p in pages]
        seen["units"] = kw.get("units")
        seen["marked"] = [p["page"] for p in pages if p["text"].startswith(recipe_picker.ONLY_COMPLETES)]
        return [ExtractedRecipe(id=f"r{n}", title=f"Rezept {n}", source_page_start=n, source_page_end=n)
                for n in (6, 8, 20, 22)], TokenUsage()  # 8 and 22 start on a page sent only to complete
    monkeypatch.setattr(main, "extract_recipes_from_pages", extract)
    monkeypatch.setattr(main, "_fetch_existing_tags", lambda: [])
    monkeypatch.setattr(main, "_mark_duplicates", lambda job: None)
    monkeypatch.setattr(main.import_matching, "match_job_ingredients", lambda job: None)
    monkeypatch.setattr(main, "guess_cookbook_title", lambda *a, **k: ("Buch", TokenUsage()))
    monkeypatch.setattr(main.target, "client", lambda: type("C", (), {
        "get_client": staticmethod(lambda: __import__("contextlib").nullcontext()),
        "fetch_all_recipe_names": staticmethod(lambda c: ["Rezept 8"])})())
    api = TestClient(main.app)
    with open(tmp_path / "c.pdf", "rb") as f:
        job_id = api.post("/api/upload", files=[("files", ("buch.pdf", f.read(), "application/pdf"))]).json()["job_id"]
    job = _wait(api, job_id, "pick")
    entries = {e["title"]: e for e in job["pick"]["entries"]}
    assert entries["Rezept 8"]["existing"] == "Rezept 8"  # in the collection already
    assert entries["Rezept 6"]["tokens"] > 0 and job["pick"]["source"] == "bookmarks"
    assert entries["Rezept 4"]["existing"] is None  # not "Rezept 8"
    assert entries["Rezept 6"]["recipe"] and not entries["Vorwort"]["recipe"] and not entries["Register"]["recipe"]
    assert api.post(f"/api/jobs/{job_id}/pick", json={"ids": []}).status_code == 400
    api.post(f"/api/jobs/{job_id}/pick", json={"ids": [entries["Rezept 6"]["id"], entries["Rezept 20"]["id"]]})
    job = _wait(api, job_id, "ready")
    assert seen["pages"] == [6, 7, 8, 20, 21, 22]  # each with the page after it
    assert seen["marked"] == [8, 22]
    assert seen["units"] == [1, 4]  # the AI calls are cut where each picked recipe starts
    assert [r["title"] for r in job["recipes"]] == ["Rezept 6", "Rezept 20"]


def test_all_reads_the_whole_book(tmp_path, monkeypatch):
    cookbook(tmp_path / "d.pdf")
    seen = {}
    monkeypatch.setattr(main, "extract_recipes_from_pages",
                        lambda pages, **kw: (seen.setdefault("n", len(pages)), ([], TokenUsage()))[1])
    monkeypatch.setattr(main, "_fetch_existing_tags", lambda: [])
    monkeypatch.setattr(main, "_mark_duplicates", lambda job: None)
    monkeypatch.setattr(main.import_matching, "match_job_ingredients", lambda job: None)
    monkeypatch.setattr(main, "guess_cookbook_title", lambda *a, **k: ("Buch", TokenUsage()))
    api = TestClient(main.app)
    with open(tmp_path / "d.pdf", "rb") as f:
        job_id = api.post("/api/upload", files=[("files", ("buch.pdf", f.read(), "application/pdf"))]).json()["job_id"]
    _wait(api, job_id, "pick")
    api.post(f"/api/jobs/{job_id}/pick", json={"all": True})
    assert _wait(api, job_id, "ready")["status"] == "ready"  # finished before the test ends
    assert seen["n"] == 24


def _wait(api, job_id, status):
    for _ in range(300):
        job = api.get(f"/api/jobs/{job_id}").json()
        if job["status"] in (status, "error"):
            return job
        time.sleep(0.02)
    return job


def test_epub_table_of_contents(tmp_path):
    from ebooklib import epub
    from app.epub_processor import process_epub
    book = epub.EpubBook()
    book.set_identifier("x")
    book.set_title("Buch")
    chapters = []
    for n, title in enumerate(["Vorwort", "Shakshuka", "Linsensuppe", "Brot", "Kuchen", "Pesto"], 1):
        body = RECIPE.format(title=title) if n > 1 else STORY
        c = epub.EpubHtml(title=title, file_name=f"c{n}.xhtml", content=f"<h1>{title}</h1><p>{body.replace(chr(10), '<br/>')}</p>")
        book.add_item(c)
        chapters.append(c)
    book.toc = [chapters[0], (epub.Section("Rezepte"), chapters[1:])]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = chapters
    epub.write_epub(str(tmp_path / "b.epub"), book)
    result = process_epub(str(tmp_path / "b.epub"), str(tmp_path / "img"))
    found, source = recipe_picker.entries(result)
    assert source == "contents" and [e["title"] for e in found] == ["Vorwort", "Shakshuka", "Linsensuppe", "Brot", "Kuchen", "Pesto"]
    assert found[0]["group"] is None and found[1]["group"] == "Rezepte"
    entries = recipe_picker.with_tokens(found, result["pages"], [])
    assert [e["recipe"] for e in entries] == [False, True, True, True, True, True]
    assert recipe_picker.worth_asking("epub", result, found)


def test_picked_recipes_are_never_read_twice():
    from app.ai_extractor import _chunk_pages
    pages = [{"page": n, "text": "x"} for n in range(1, 31)]
    units = list(range(1, 31, 3))  # ten recipes of three pages each
    chunks = [[p["page"] for p in c] for c in _chunk_pages(pages, units=units)]
    assert sum(len(c) for c in chunks) == 30  # no page twice
    assert all(c[0] in units for c in chunks) and all(len(c) <= 12 for c in chunks)


def test_the_largest_photo_on_its_pages_is_the_preview():
    found = [{"id": "e0", "start": 4, "end": 5}, {"id": "e1", "start": 6, "end": 6}, {"id": "e2", "start": 7, "end": 7}]
    images = {"small": {"page": 4, "width": 300, "height": 200}, "big": {"page": 5, "width": 900, "height": 600},
              "other": {"page": 6, "width": 400, "height": 400}}
    assert recipe_picker.best_photos(found, images) == {"e0": "big", "e1": "other"}  # e2: its page instead


def test_pick_thumb_renders_the_page_without_a_photo(tmp_path, monkeypatch):
    cookbook(tmp_path / "t.pdf")
    monkeypatch.setattr(main, "_fetch_existing_tags", lambda: [])
    api = TestClient(main.app)
    with open(tmp_path / "t.pdf", "rb") as f:
        job_id = api.post("/api/upload", files=[("files", ("buch.pdf", f.read(), "application/pdf"))]).json()["job_id"]
    job = _wait(api, job_id, "pick")
    entry = job["pick"]["entries"][1]
    resp = api.get(f"/api/jobs/{job_id}/pick-thumb/{entry['id']}")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg" and resp.content[:2] == b"\xff\xd8"
    assert api.get(f"/api/jobs/{job_id}/pick-thumb/nope").status_code == 404
    api.post(f"/api/jobs/{job_id}/pick", json={"cancel": True})
