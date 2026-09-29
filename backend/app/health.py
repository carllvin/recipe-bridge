"""Collection health overview for the maintenance page: what each tool would
find - without any AI call. Computing it reads every recipe in full (one
request per recipe), so it runs in the background on request and the last
result (the affected entries per metric) is cached in the data volume.
Entries the user ignored (see ignored.py) are left out of the counts when
reading the cache, so ignoring takes effect without recomputing."""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from . import cook_today, duplicates, mealie_client, mealie_maintenance, target, tools_tag_groups, tools_unused, ignored, tools_recipe_details, recipe_restructure, tandoor_client, tool_jobs, tools_conversions, tools_ingredients, tools_recipes, tools_tags
from .config import get_language_code, settings
from .tandoor_helpers import fetch_all_recipes_full

log = logging.getLogger("recipe-bridge")

# changed_at: when a suggestion was last applied or recipes were imported -
# a cached result computed before that is stale and the page refreshes it.
_state = {"running": False, "error": None, "changed_at": 0.0}
_lock = threading.Lock()


def _path() -> str:
    return os.path.join(settings.data_dir, "health.json")


def _read() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"computed_at": None, "metrics": {}}


def cached() -> dict:
    data = _read()
    metrics = dict(data.get("metrics", {}))
    ignored_all = ignored.load()
    ignored_counts = {m: len(ignored_all.get(m, {})) for m in ignored.METRICS}
    for metric, items in (data.get("items") or {}).items():
        skip = set(ignored_all.get(metric, {}))
        metrics[metric] = sum(1 for item in items if item["key"] not in skip)
    computed_at = data.get("computed_at")
    return {"computed_at": computed_at, "metrics": metrics, "ignored": ignored_counts,
            "stale": bool(computed_at) and _state["changed_at"] > computed_at,
            "pending": _pending_by_tool(),
            "running": _state["running"], "error": _state["error"]}


def mark_changed() -> None:
    """Called after something changed the collection (applied suggestion,
    import) - the overview then counts as stale."""
    _state["changed_at"] = time.time()


def _pending_by_tool() -> dict:
    """Per tool, the newest run that is still scanning or has suggestions
    waiting for review: {"job_id", "scanning", "count"} - so a tile can show
    that its fix is already under way."""
    result = {}
    for job in sorted(tool_jobs.list_all_tool_jobs(), key=lambda j: j.created_at):
        count = sum(1 for s in job.suggestions if s.status == "pending")
        scanning = job.status == "scanning"
        if scanning or count:
            result[job.tool] = {"job_id": job.id, "scanning": scanning, "count": count}
    return result


def items(metric: str) -> dict:
    """The affected entries of one metric (without the ignored ones) and the
    ignored ones, as [{"key", "name", "recipe_id"?}]."""
    if metric not in ignored.METRICS:
        raise ValueError(f"Unknown metric {metric!r}")
    skip = ignored.load().get(metric, {})
    listed = [item for item in (_read().get("items") or {}).get(metric, []) if item["key"] not in skip]
    ignored_items = [{"key": key, "name": name} for key, name in skip.items()]
    if metric.startswith("recipes_"):
        for item in ignored_items:
            item["recipe_id"] = int(item["key"]) if item["key"].isdigit() else None
    return {"items": listed, "ignored": sorted(ignored_items, key=lambda i: i["name"].casefold())}


def compute_now() -> None:
    """Blocking refresh (for the maintenance run); waits for one that's
    already going instead of starting a second."""
    with _lock:
        busy = _state["running"]
        if not busy:
            _state["running"], _state["error"] = True, None
    if not busy:
        _compute()
        return
    while _state["running"]:
        time.sleep(2)


def start_refresh() -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state["running"], _state["error"] = True, None
    threading.Thread(target=_compute, daemon=True).start()
    return True


def _unused_items(recipes, foods, units, keywords, unit_conversions) -> dict:
    used = tools_unused.used_ids(recipes)
    protected = {"unit": tools_unused.protected_unit_ids(foods.values(), unit_conversions)}
    items = {"food": list(foods.values()), "unit": units, "keyword": keywords}
    return {
        metric: [{"key": str(i["id"]), "name": i.get("name", "")}
                 for i in tools_unused.find_unused(endpoint, items[endpoint], used[endpoint], protected.get(endpoint, ()))]
        for endpoint, (metric, _) in tools_unused.KINDS.items()
    }


def _write(started, metrics, item_lists, recipe_versions=None) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"computed_at": started, "metrics": metrics, "items": item_lists,
                   "recipe_versions": recipe_versions}, f, ensure_ascii=False)
    os.replace(tmp, _path())


def _compute() -> None:
    started = time.time()  # changes during the run make the result stale again
    if target.is_mealie():  # the tiles that work with Mealie (mealie_maintenance)
        try:
            with mealie_client.get_client() as client:
                metrics, item_lists = mealie_maintenance.compute(client)
            _write(started, metrics, item_lists)
        except Exception as exc:  # noqa: BLE001
            log.exception("Health overview (Mealie) failed")
            _state["error"] = str(exc)
        finally:
            _state["running"] = False
        return
    try:
        with tandoor_client.get_client() as client:
            recipes = fetch_all_recipes_full(client)
            foods = {f["id"]: f for f in tools_ingredients.fetch_all_foods_full(client)}
            categories = tools_ingredients.fetch_supermarket_categories(client)
            food_names = tools_tags.food_name_set(client)
            used_foods = {
                (ing.get("food") or {}).get("id")
                for r in recipes for step in r.get("steps", []) for ing in step.get("ingredients", [])
            } - {None}
            units = tools_conversions._fetch_all(client, "unit")
            keywords = tools_conversions._fetch_all(client, "keyword")
            unit_conversions = tools_conversions._fetch_all(client, "unit-conversion")
            cook_today.save_index(recipes)  # "what can I cook today?" reuses this full read
            general, to_estimate = tools_conversions.find_missing(
                client, tools_conversions.recipe_pairs(recipes), respect_ignored=False)

        expected = get_language_code(settings.output_language)

        def food_items(missing):
            return sorted(({"key": str(fid), "name": foods[fid]["name"]} for fid in used_foods
                           if fid in foods and missing(foods[fid])), key=lambda i: i["name"].casefold())

        def recipe_items(matches):
            return [{"key": str(r["id"]), "name": r.get("name", ""), "recipe_id": r["id"]}
                    for r in sorted(recipes, key=lambda r: (r.get("name") or "").casefold()) if matches(r)]

        conversion_items = [
            {"key": f"*:{sug.detail['base_unit']['id']}",
             "name": f"{sug.detail['base_unit']['name']} → {sug.detail['converted_unit']['name']}"}
            for sug in general
        ] + [
            {"key": f"{p['food']['id']}:{p['unit']['id']}",
             "name": f"{p['food']['name']}: {p['unit']['name']} → {p['target']['name']}"}
            for p in to_estimate
        ]
        item_lists = {
            "foods_without_nutrition": food_items(lambda f: not f.get("properties")),
            "foods_without_category": food_items(lambda f: not f.get("supermarket_category")) if categories else [],
            "missing_conversions": conversion_items,
            "foods_duplicates": [{"key": p["key"], "name": p["name"]} for p in duplicates.food_duplicates(foods.values())],
            "units_duplicates": [{"key": p["key"], "name": p["name"]} for p in duplicates.unit_duplicates(units)],
            **_unused_items(recipes, foods, units, keywords, unit_conversions),
            "keywords_ungrouped": [{"key": str(k["id"]), "name": k.get("name", "")}
                                   for k in tools_tag_groups.ungrouped(keywords)],
            "recipes_not_translated": recipe_items(lambda r: not tools_recipes.already_in_target_language(r, expected)),
            "recipes_need_restructure": recipe_items(lambda r: bool(recipe_restructure.needs_restructure(r))),
            "recipes_without_season": recipe_items(lambda r: not tools_tags.has_season_tag(r)),
            "recipes_without_servings": recipe_items(tools_recipe_details.lacks_servings),
            "recipes_without_image": recipe_items(tools_recipe_details.lacks_image),
            "recipes_few_tags": recipe_items(
                lambda r: sum(1 for kw in r.get("keywords", []) if kw["name"].strip().casefold() not in food_names)
                < tools_tags.MIN_TAGS_DEFAULT
            ),
        }
        metrics = {"recipes_total": len(recipes), "foods_used": len(used_foods)}
        # lets the recipe tools read only what changed since (recipe_scope.py)
        recipe_versions = {str(r["id"]): r.get("updated_at") for r in recipes}
        _write(started, metrics, item_lists, recipe_versions)
    except Exception as exc:  # noqa: BLE001
        log.exception("Health overview failed")
        _state["error"] = str(exc)
    finally:
        _state["running"] = False
