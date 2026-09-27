import time

from app import app_settings, health, maintenance, tool_jobs, usage_log


def test_settings_are_validated():
    saved = app_settings.update({
        "maintenance": {"enabled": True, "hour": 30, "every_days": 0, "metrics": ["missing_conversions", "bogus"]},
        "budget": {"monthly_tokens": -5},
    })
    assert saved["maintenance"] == {"enabled": True, "hour": 23, "every_days": 1, "metrics": ["missing_conversions"]}
    assert saved["budget"]["monthly_tokens"] == 0
    assert app_settings.get() == saved


def test_budget_blocks_automatic_and_optionally_manual_runs():
    app_settings.update({"budget": {"monthly_tokens": 1000}})
    assert usage_log.automatic_runs_allowed()
    usage_log.record("x", 900, 200)
    status = usage_log.budget_status()
    assert status["used"] == 1100 and status["exceeded"]
    assert not usage_log.automatic_runs_allowed()
    assert usage_log.manual_runs_allowed()
    app_settings.update({"budget": {"block_manual": True}})
    assert not usage_log.manual_runs_allowed()


def test_next_run_follows_hour_and_interval():
    cfg = {"enabled": True, "hour": 3, "every_days": 2, "metrics": []}
    last = time.mktime((2026, 9, 20, 3, 0, 0, 0, 0, -1))
    assert time.localtime(maintenance.next_run_at(cfg, last_run_at=last))[:4] == (2026, 9, 22, 3)
    assert maintenance.next_run_at({**cfg, "enabled": False}) is None
    first = maintenance.next_run_at(cfg, last_run_at=0)
    assert first > time.time() and time.localtime(first).tm_hour == 3


def test_maintenance_runs_selected_tools_and_stops_on_budget(monkeypatch):
    app_settings.update({"maintenance": {"metrics": ["missing_conversions", "recipes_without_season", "foods_without_category"]}})
    monkeypatch.setattr(health, "compute_now", lambda: None)
    monkeypatch.setattr(health, "cached", lambda: {
        "metrics": {"missing_conversions": 3, "recipes_without_season": 0, "foods_without_category": 2},
        "pending": {"ingredients_enrich": {"job_id": "x", "scanning": False, "count": 4}}})
    ran = []
    maintenance.configure({"conversions": lambda job_id: ran.append(tool_jobs.get_tool_job(job_id).meta)})
    assert maintenance.run_once()
    # conversions ran; season had nothing to do; enrich still waits for review
    assert ran == [{"auto": True, "trigger": "maintenance"}]
    assert maintenance.status()["last_result"]["skipped"] == ["ingredients_enrich"]

    app_settings.update({"budget": {"monthly_tokens": 10}})
    usage_log.record("x", 20, 0)
    ran.clear()
    maintenance.run_once()
    assert ran == [] and maintenance.status()["last_result"]["budget_stop"]
