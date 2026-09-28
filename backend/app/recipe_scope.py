"""Which recipes a recipe tool has to look at.

The health overview (health.py) already read every recipe in full and
stored, per tile, the recipes it affects - plus each recipe's "updated_at".
A tool behind such a tile doesn't need to read the whole collection again:
it reads the compact recipe list (a few requests), and fetches in full only
- the recipes the tile listed,
- recipes that are new or were changed since the overview was computed
  (by "updated_at"; for an overview saved before that was stored, by
  comparing the times).
Each of those is checked again with the tool's own rule. Without a stored
overview it falls back to reading everything."""
from __future__ import annotations

import datetime as dt
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


def _changed_after(item, timestamp) -> bool:
    """True when the recipe was created or edited after timestamp (epoch
    seconds) - or when Tandoor doesn't say (then it's rather read once too
    often)."""
    for field in ("updated_at", "created_at"):
        value = item.get(field)
        if not value:
            continue
        try:
            when = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return True
        if when.tzinfo is None:
            when = when.astimezone()  # naive = the server's local time
        # a minute of slack for clocks that aren't exactly in sync
        return when.timestamp() > timestamp - 60
    return True


def _is_changed(item, versions, computed_at) -> bool:
    """New or changed since the overview."""
    key = str(item["id"])
    if versions is not None:
        return key not in versions or versions[key] != item.get("updated_at")
    return _changed_after(item, computed_at)  # overview from an older version: compare times


def changed_since_overview(client, job=None):
    """(the overview's entries per metric, the full recipes that are new or
    changed since it was computed) - or None when there is no overview.
    For tools about entries (ingredients, units, tags) rather than recipes."""
    data = _health()
    versions, computed_at = data.get("recipe_versions"), data.get("computed_at")
    if not data.get("items") or (versions is None and not computed_at):
        return None
    changed = [item["id"] for item in fetch_recipe_overview(client) if _is_changed(item, versions, computed_at)]
    if job is not None:
        job.progress_label = f"Reading {len(changed)} changed recipe(s)..."
        tool_jobs.save_tool_job(job)
    return data["items"], fetch_recipes_full(client, changed)


def recipes_for(client, metric: str, matches, skip=frozenset(), job=None) -> list[dict]:
    """Full recipes (not in skip) for which matches(recipe) is true."""
    data = _health()
    listed = (data.get("items") or {}).get(metric)
    versions = data.get("recipe_versions")
    computed_at = data.get("computed_at")
    if listed is None or (versions is None and not computed_at):
        if job is not None:
            job.progress_label = "Scanning every recipe's full detail..."
            tool_jobs.save_tool_job(job)
        return [r for r in fetch_all_recipes_full(client) if str(r["id"]) not in skip and matches(r)]

    ids = {item["key"] for item in listed}
    overview = fetch_recipe_overview(client)
    ids |= {str(item["id"]) for item in overview if _is_changed(item, versions, computed_at)}
    todo = [item["id"] for item in overview if str(item["id"]) in ids and str(item["id"]) not in skip]
    if job is not None:
        job.progress_label = f"Reading {len(todo)} recipe(s)..."
        tool_jobs.save_tool_job(job)
    log.info("%s: reading %d of %d recipes (from the health overview)", metric, len(todo), len(overview))
    return [r for r in fetch_recipes_full(client, todo) if matches(r)]
