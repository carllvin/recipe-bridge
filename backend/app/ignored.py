"""Entries the user chose to ignore in the collection health overview -
e.g. "Wasser" without nutrition values, or a recipe that deliberately has
no season tag. Stored per health metric as {key: name} in the data volume.
Ignored entries don't count in the overview and the tools that fix that
metric don't suggest them any more.

Keys are strings: the food or recipe id, "<food id>:<unit id>" for a
missing conversion ("*:<unit id>" for a general one), or "<id>:<id>" (smaller
id first) for a pair of likely duplicates that are rightly separate."""
from __future__ import annotations

import json
import os
import threading

from .config import settings

METRICS = {
    "foods_without_nutrition", "foods_without_category", "missing_conversions",
    "recipes_not_translated", "recipes_need_restructure", "recipes_amounts_missing", "recipes_without_season", "recipes_few_tags",
    "foods_duplicates", "units_duplicates", "recipes_without_servings", "recipes_without_image",
    "foods_unused", "units_unused", "keywords_unused", "keywords_ungrouped",
}

_lock = threading.Lock()


def _path() -> str:
    return os.path.join(settings.data_dir, "ignored.json")


def load() -> dict[str, dict[str, str]]:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())


def keys(metric: str) -> set[str]:
    return set(load().get(metric, {}))


def add(metric: str, items: list[dict]) -> None:
    if metric not in METRICS:
        raise ValueError(f"Unknown metric {metric!r}")
    with _lock:
        data = load()
        entries = data.setdefault(metric, {})
        for item in items:
            key = str(item.get("key") or "").strip()
            if key:
                entries[key] = str(item.get("name") or key)
        _save(data)


def remove(metric: str, item_keys: list[str]) -> None:
    with _lock:
        data = load()
        entries = data.get(metric, {})
        for key in item_keys:
            entries.pop(str(key), None)
        _save(data)
