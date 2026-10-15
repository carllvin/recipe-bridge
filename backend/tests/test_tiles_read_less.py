"""Tiles with a few entries read only those - not every ingredient
(Tandoor's ingredient list is slow for large collections)."""
import json
import os
import time

from app import llm_provider, tandoor_client, tool_jobs, tools_ingredients, tools_unused
from app.config import settings


def overview(items, versions=None):
    with open(os.path.join(settings.data_dir, "health.json"), "w") as f:
        json.dump({"computed_at": time.time(), "metrics": {}, "items": items, "recipe_versions": versions or {}}, f)


def no_full_list(*a, **kw):
    raise AssertionError("the whole list was read")


def test_duplicates_tile_reads_only_its_pairs(tandoor, monkeypatch):
    for fid, name in [(1, "Zwiebel"), (2, "Zwiebeln"), (3, "Mehl"), (4, "Salz")]:
        tandoor.add("food", {"id": fid, "name": name, "plural_name": "Zwiebeln" if fid == 1 else ""})
    overview({"foods_duplicates": [{"key": "1:2", "name": "Zwiebel ↔ Zwiebeln"}]})
    monkeypatch.setattr(tools_ingredients, "fetch_all_foods_full", no_full_list)
    monkeypatch.setattr(tandoor_client, "fetch_all_items", no_full_list)
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    seen = []

    def ai(system, content, max_tokens=0):
        seen.append(json.loads(content))
        return json.dumps([{"type": "merge", "keep_id": 1, "remove_ids": [2], "keep_name": "Zwiebel"}]), None
    monkeypatch.setattr(llm_provider, "complete_tool_text", ai)
    job = tool_jobs.create_tool_job("ingredients_review")
    job.meta["focus"] = "duplicates"
    tool_jobs.save_tool_job(job)
    tools_ingredients.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert job.status == "ready", job.error
    assert seen == [[{"id": 1, "name": "Zwiebel"}, {"id": 2, "name": "Zwiebeln"}]]
    assert [s.kind for s in job.suggestions] == ["merge"]


def test_unused_tile_reads_only_its_entries(tandoor, monkeypatch):
    tandoor.add("food", {"id": 7, "name": "Kerbel"})
    tandoor.add("food", {"id": 8, "name": "Mehl"})
    overview({"foods_unused": [{"key": "7", "name": "Kerbel"}, {"key": "99", "name": "längst gelöscht"}]})
    monkeypatch.setattr(tools_unused, "_fetch_all", no_full_list)
    job = tool_jobs.create_tool_job("unused_foods")
    tool_jobs.save_tool_job(job)
    tools_unused.run_scan(job.id)
    job = tool_jobs.get_tool_job(job.id)
    assert job.status == "ready", job.error
    assert [s.detail["name"] for s in job.suggestions] == ["Kerbel"]


# ---------- after applying: the tile, not a new scan ----------

def test_applied_suggestions_leave_their_tile(monkeypatch):
    from app import health
    from app.schemas import ToolSuggestion
    overview({
        "foods_duplicates": [{"key": "1:2", "name": "Zwiebel ↔ Zwiebeln"}, {"key": "3:4", "name": "Mehl ↔ Mehle"}],
        "foods_without_nutrition": [{"key": "2", "name": "Zwiebeln"}, {"key": "5", "name": "Salz"}],
        "recipes_without_season": [{"key": "10", "name": "Suppe"}, {"key": "11", "name": "Kuchen"}],
        "recipes_without_source": [{"key": "10", "name": "Suppe"}, {"key": "12", "name": "Brot"}],
    })
    monkeypatch.setattr(health, "_state", {**health._state, "changed_at": 0})

    def apply(tool, kind, detail):
        health.settle(tool, ToolSuggestion(id="x", kind=kind, summary="", detail=detail))

    apply("ingredients_review", "merge", {"type": "merge", "keep_id": 1, "remove_ids": [2], "keep_name": "Zwiebel"})
    apply("tags_season", "season", {"recipe_id": 10, "season": "Winter"})
    apply("recipes_source", "source_tag", {"tag": "x", "recipe_ids": [10, 12]})
    data = health.cached()
    assert data["metrics"]["foods_duplicates"] == 1  # only Mehl ↔ Mehle is left
    assert data["metrics"]["foods_without_nutrition"] == 1  # the merged-away Zwiebeln is gone
    assert data["metrics"]["recipes_without_season"] == 1 and data["metrics"]["recipes_without_source"] == 0
    assert not data["stale"]  # nothing to scan again


def test_what_cannot_be_told_still_marks_stale(monkeypatch):
    from app import health
    from app.schemas import ToolSuggestion
    overview({"missing_conversions": [{"key": "1:2", "name": "x"}]})
    monkeypatch.setattr(health, "_state", {**health._state, "changed_at": 0})
    health.settle("conversions", ToolSuggestion(id="x", kind="conversion", summary="", detail={}))
    assert health.cached()["stale"]


def test_overview_reads_each_list_once(tandoor, caplog):
    import logging
    from collections import Counter
    from app import health
    tandoor.add("food", {"id": 1, "name": "Mehl"})
    tandoor.add("unit", {"id": 1, "name": "g"})
    tandoor.add("recipe", {"id": 1, "name": "Brot", "keywords": [], "steps": [
        {"instruction": "Backen.", "ingredients": [{"food": {"id": 1}, "unit": {"id": 1}, "amount": 500}]}]})
    with caplog.at_level(logging.INFO, logger="recipe-bridge"):
        health.compute_now()
    assert health.cached()["metrics"]["recipes_total"] == 1
    lists = Counter(path for method, path in tandoor.requests if method == "GET" and path.count("/") == 2)
    for path in ("/food/", "/unit/", "/keyword/", "/unit-conversion/"):
        assert lists[path] == 1, (path, lists)
    assert any("Collection overview:" in r.message and "foods" in r.message for r in caplog.records)


def test_long_lists_read_page_by_page_at_once():
    import httpx
    items = [{"id": i, "name": f"F{i}"} for i in range(450)]
    seen = []

    def handler(request):
        page, size = int(request.url.params.get("page", 1)), int(request.url.params["page_size"])
        seen.append(page)
        chunk = items[(page - 1) * size:page * size]
        more = page * size < len(items)
        return httpx.Response(200, json={"count": len(items), "results": chunk,
                                         "next": f"http://tandoor/api/food/?page={page + 1}" if more else None})
    client = httpx.Client(base_url="http://tandoor/api", transport=httpx.MockTransport(handler))
    assert tandoor_client.fetch_pages(client, "/food/") == items
    assert sorted(seen) == [1, 2, 3]


def test_an_overview_from_before_the_start_is_recounted():
    from app import health
    overview({}, {})  # written "now" - after the app's start
    assert not health.cached()["stale"]
    with open(os.path.join(settings.data_dir, "health.json"), "w") as f:
        json.dump({"computed_at": health._state["changed_at"] - 60, "metrics": {}, "items": {}}, f)
    assert health.cached()["stale"]  # e.g. computed by the version before an update
