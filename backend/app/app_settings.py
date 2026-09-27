"""Settings changed in the UI (unlike .env, which needs a restart): the
automatic maintenance schedule and the monthly AI budget. Stored as
settings.json in the data volume."""
from __future__ import annotations

import copy
import json
import os
import threading

from .config import settings

MAINTENANCE_METRICS = [
    "foods_duplicates", "foods_without_nutrition", "foods_without_category", "missing_conversions",
    "units_duplicates", "recipes_not_translated", "recipes_need_restructure", "recipes_without_season",
    "recipes_few_tags", "recipes_without_servings", "recipes_without_image",
]

DEFAULTS = {
    "maintenance": {
        "enabled": False,
        "hour": 3,           # local time of the container (TZ)
        "every_days": 1,
        # Cheap, well-contained fixes by default; restructuring and tag
        # suggestions cost more and are opt-in.
        "metrics": ["foods_without_nutrition", "foods_without_category", "missing_conversions",
                    "recipes_without_season"],
    },
    "budget": {
        "monthly_tokens": 0,    # 0 = no limit
        "block_manual": False,  # also refuse tool starts and imports once reached
    },
}

_lock = threading.Lock()


def _path() -> str:
    return os.path.join(settings.data_dir, "settings.json")


def get() -> dict:
    result = copy.deepcopy(DEFAULTS)
    try:
        with open(_path(), encoding="utf-8") as f:
            stored = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        stored = {}
    for section, values in result.items():
        if isinstance(stored.get(section), dict):
            values.update({k: v for k, v in stored[section].items() if k in values})
    return result


def update(changes: dict) -> dict:
    """Merges and validates `changes` ({"maintenance": {...}, "budget": {...}})."""
    with _lock:
        current = get()
        m = {**current["maintenance"], **(changes.get("maintenance") or {})}
        b = {**current["budget"], **(changes.get("budget") or {})}
        current["maintenance"] = {
            "enabled": bool(m["enabled"]),
            "hour": min(23, max(0, int(m["hour"]))),
            "every_days": min(30, max(1, int(m["every_days"]))),
            "metrics": [x for x in MAINTENANCE_METRICS if x in (m["metrics"] or [])],
        }
        current["budget"] = {
            "monthly_tokens": max(0, int(b["monthly_tokens"] or 0)),
            "block_manual": bool(b["block_manual"]),
        }
        os.makedirs(settings.data_dir, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=1)
        os.replace(tmp, _path())
        return current
