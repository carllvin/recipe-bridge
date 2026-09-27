"""Health overview with ignored entries, and the inbox API with failed
suggestions (retry / dismiss)."""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import health, ignored, main, tool_jobs, tools_ingredients
from app.config import settings
from app.schemas import ToolSuggestion


def write_health(items):
    with open(os.path.join(settings.data_dir, "health.json"), "w") as f:
        json.dump({"computed_at": time.time(), "metrics": {"recipes_total": 3}, "items": items}, f)


def test_ignored_entries_do_not_count():
    write_health({"foods_without_nutrition": [{"key": "1", "name": "Wasser"}, {"key": "2", "name": "Salz"}]})
    assert health.cached()["metrics"]["foods_without_nutrition"] == 2
    ignored.add("foods_without_nutrition", [{"key": "1", "name": "Wasser"}])
    data = health.cached()
    assert data["metrics"]["foods_without_nutrition"] == 1 and data["ignored"]["foods_without_nutrition"] == 1
    assert health.items("foods_without_nutrition") == {"items": [{"key": "2", "name": "Salz"}],
                                                       "ignored": [{"key": "1", "name": "Wasser"}]}
    ignored.remove("foods_without_nutrition", ["1"])
    assert health.cached()["metrics"]["foods_without_nutrition"] == 2


def test_tools_skip_ignored_foods():
    ignored.add("foods_without_nutrition", [{"key": "1", "name": "Wasser"}])
    foods = [{"id": 1, "name": "Wasser", "plural_name": "x", "properties": []},
             {"id": 2, "name": "Salz", "plural_name": "x", "properties": []}]
    assert [t["id"] for t in tools_ingredients.enrich_targets(foods, [])] == [2]


def test_changes_mark_the_overview_stale():
    write_health({})
    assert not health.cached()["stale"]
    time.sleep(0.01)
    health.mark_changed()
    assert health.cached()["stale"]


@pytest.fixture
def api():
    return TestClient(main.app)  # without "with": no startup loops


def test_failed_suggestions_can_be_retried_and_dismissed(api, monkeypatch):
    job = tool_jobs.create_tool_job("conversions")
    job.status = "ready"
    job.suggestions = [ToolSuggestion(id="c1", kind="conversion", summary="Zwiebel: 1 EL = 10 g", status="error", error="400 kaputt")]
    tool_jobs.save_tool_job(job)

    inbox = api.get("/api/inbox").json()
    assert [(i["id"], i["failed"], i["error"], i["retryable"]) for i in inbox["items"]] == [("c1", True, "400 kaputt", True)]
    assert api.get("/api/inbox/count").json()["count"] == 1

    attempts = []

    def fake_apply(job_id, suggestion_id):
        s = tool_jobs.get_tool_job(job_id).suggestions[0]
        attempts.append(s.status)  # it's pending again when the retry applies it
        s.status = "applied"
        return s
    monkeypatch.setitem(main._TOOL_APPLY, "conversions", fake_apply)
    assert main._perform_suggestion_action(job.id, "c1", "retry").status == "applied"
    assert attempts == ["pending"]

    job.suggestions[0].status = "error"
    assert main._perform_suggestion_action(job.id, "c1", "skip").status == "skipped"
    assert api.get("/api/inbox").json()["items"] == []


def test_budget_blocks_manual_starts_when_configured(api):
    from app import app_settings, usage_log
    app_settings.update({"budget": {"monthly_tokens": 10, "block_manual": True}})
    usage_log.record("x", 20, 0)
    resp = api.post("/api/tools/conversions")
    assert resp.status_code == 409 and "budget" in resp.json()["detail"]
