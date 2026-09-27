"""Automatic maintenance (settings in the UI, see app_settings.py): at the
configured hour, every N days, recount the collection health overview and
start the tool for each selected tile that has something to do. The tools
only prepare suggestions - nothing is changed before you review them under
"Review". Runs one tool after another, skips tools that still have a run
in progress or waiting for review, and stops once the monthly AI budget is
used up."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time

from . import app_settings, health, tool_jobs, usage_log
from .config import settings

log = logging.getLogger("tandoor-helper")

# health metric -> (tool, extra job meta)
METRIC_TOOLS = {
    "foods_duplicates": ("ingredients_review", {"focus": "duplicates"}),
    "foods_without_nutrition": ("ingredients_enrich", {}),
    "foods_without_category": ("ingredients_enrich", {}),
    "missing_conversions": ("conversions", {}),
    "units_duplicates": ("units_review", {"focus": "duplicates"}),
    "recipes_not_translated": ("recipes_translate", {}),
    "recipes_need_restructure": ("recipes_restructure", {}),
    "recipes_without_season": ("tags_season", {}),
    "recipes_few_tags": ("tags_suggest_more", {}),
    "recipes_without_servings": ("recipes_servings", {}),
    "recipes_without_image": ("recipes_images", {}),  # costs per image only when applied
}

_scans: dict = {}
_lock = threading.Lock()
_running = {"active": False, "label": None}


def configure(scans: dict) -> None:
    _scans.update(scans)


def _path() -> str:
    return os.path.join(settings.data_dir, "maintenance.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(data, f)


def next_run_at(cfg: dict | None = None, last_run_at: float | None = None) -> float | None:
    cfg = cfg or app_settings.get()["maintenance"]
    if not cfg["enabled"]:
        return None
    last_run_at = _load().get("last_run_at") if last_run_at is None else last_run_at
    now = time.time()
    base = time.localtime(last_run_at or now)
    days = cfg["every_days"] if last_run_at else 0
    at = time.mktime((base.tm_year, base.tm_mon, base.tm_mday + days, cfg["hour"], 0, 0, 0, 0, -1))
    if not last_run_at and at <= now:
        at = time.mktime((base.tm_year, base.tm_mon, base.tm_mday + 1, cfg["hour"], 0, 0, 0, 0, -1))
    return at


def status() -> dict:
    data = _load()
    return {"next_run_at": next_run_at(), "last_run_at": data.get("last_run_at"),
            "last_result": data.get("last_result"), "running": _running["active"], "label": _running["label"]}


def run_once(trigger: str = "schedule") -> bool:
    """Blocking. Returns False if a maintenance run is already going."""
    with _lock:
        if _running["active"]:
            return False
        _running["active"] = True
    started, skipped, budget_stop = [], [], False
    try:
        cfg = app_settings.get()["maintenance"]
        _running["label"] = "health"
        health.compute_now()
        overview = health.cached()
        tools = []
        for metric in cfg["metrics"]:
            if overview["metrics"].get(metric) and METRIC_TOOLS[metric] not in tools:
                tools.append(METRIC_TOOLS[metric])
        for tool, meta in tools:
            if not usage_log.automatic_runs_allowed():
                budget_stop = True
                break
            if tool in health.cached()["pending"]:
                skipped.append(tool)  # its last run is still waiting for review
                continue
            job = tool_jobs.create_tool_job(tool)
            job.meta.update({**meta, "auto": True, "trigger": "maintenance"})
            tool_jobs.save_tool_job(job)
            _running["label"] = tool
            log.info("Automatic maintenance: %s (job %s)", tool, job.id)
            _scans[tool](job.id)  # blocking - one tool after another
            started.append(tool)
    except Exception as exc:  # noqa: BLE001
        log.exception("Automatic maintenance failed")
        skipped.append(f"error: {exc}")
    finally:
        _save({"last_run_at": time.time(), "last_result": {
            "trigger": trigger, "started": started, "skipped": skipped, "budget_stop": budget_stop}})
        _running["active"], _running["label"] = False, None
    return True


def start_now() -> bool:
    if _running["active"]:
        return False
    threading.Thread(target=run_once, args=("manual",), daemon=True).start()
    return True


async def loop() -> None:
    await asyncio.sleep(90)  # let the app finish starting up first
    while True:
        try:
            at = next_run_at()
            if at and time.time() >= at and not _running["active"]:
                if usage_log.automatic_runs_allowed():
                    await asyncio.to_thread(run_once)
                else:
                    log.info("Automatic maintenance skipped - monthly AI budget reached")
                    _save({"last_run_at": time.time(), "last_result": {
                        "trigger": "schedule", "started": [], "skipped": [], "budget_stop": True}})
        except Exception:  # noqa: BLE001
            log.exception("Maintenance scheduler failed")
        await asyncio.sleep(60)
