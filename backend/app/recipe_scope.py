"""Which recipes a recipe tool has to look at.

The health overview (health.py) already read every recipe in full and
stored, per tile, the recipes it affects - plus each recipe's "updated_at".
A tool behind such a tile doesn't need to read the whole collection again:
it reads the compact recipe list (a few requests), and fetches in full only
- the recipes the tile listed,
- recipes that are new or were changed since the overview was computed.
Each of those is checked again with the tool's own rule. Without a stored
overview it falls back to reading everything."""
from __future__ import annotations

import json
import logging
import os

from . import tool_jobs
from .config import settings
from .tandoor_helpers import fetch_all_recipes_full, fetch_recipe_overview, fetch_recipes_full

log = logging.getLogger("tandoor-helper")


def _health() -> dict:
    try:
        with open(os.path.join(settings.data_dir, "health.json"), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def recipes_for(client, metric: str, matches, skip=frozenset(), job=None) -> list[dict]:
    """Full recipes (not in skip) for which matches(recipe) is true."""
    data = _health()
    listed = (data.get("items") or {}).get(metric)
    versions = data.get("recipe_versions")
    if listed is None or versions is None:
        if job is not None:
            job.progress_label = "Scanning every recipe's full detail..."
            tool_jobs.save_tool_job(job)
        return [r for r in fetch_all_recipes_full(client) if str(r["id"]) not in skip and matches(r)]

    ids = {item["key"] for item in listed}
    overview = fetch_recipe_overview(client)
    for item in overview:
        key = str(item["id"])
        if key not in versions or versions[key] != item.get("updated_at"):
            ids.add(key)  # new or changed since the overview
    todo = [item["id"] for item in overview if str(item["id"]) in ids and str(item["id"]) not in skip]
    if job is not None:
        job.progress_label = f"Reading {len(todo)} recipe(s)..."
        tool_jobs.save_tool_job(job)
    log.info("%s: reading %d of %d recipes (from the health overview)", metric, len(todo), len(overview))
    return [r for r in fetch_recipes_full(client, todo) if matches(r)]
