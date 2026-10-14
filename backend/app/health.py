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

from . import cook_today, duplicates, migration, photo_quality, mealie_client, mealie_maintenance, source_index, source_tag, target, tools_tag_groups, tools_unused, ignored, tools_recipe_details, recipe_amounts, recipe_restructure, tandoor_client, tool_jobs, tools_conversions, tools_ingredients, tools_recipes, tools_tags
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


def _read_parallel(timings: dict, reads: dict) -> dict:
    """{name: read()} at the same time - the overview's lists don't depend
    on each other. Each one's duration goes into timings (for the log)."""
    from concurrent.futures import ThreadPoolExecutor

    def timed(name):
        began = time.monotonic()
        try:
            return reads[name]()
        finally:
            timings[name] = time.monotonic() - began
    with ThreadPoolExecutor(max_workers=len(reads)) as pool:
        futures = {name: pool.submit(timed, name) for name in reads}
        return {name: future.result() for name, future in futures.items()}


def _unused_items(recipes, foods, units, keywords, unit_conversions) -> dict:
    used = tools_unused.used_ids(recipes)
    protected = {"unit": tools_unused.protected_unit_ids(foods.values(), unit_conversions)}
    items = {"food": list(foods.values()), "unit": units, "keyword": keywords}
    return {
        metric: [{"key": str(i["id"]), "name": i.get("name", "")}
                 for i in tools_unused.find_unused(endpoint, items[endpoint], used[endpoint], protected.get(endpoint, ()))]
        for endpoint, (metric, _) in tools_unused.KINDS.items()
    }


def _write(started, metrics, item_lists, recipe_versions=None, extra=None) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"computed_at": started, "metrics": metrics, "items": item_lists,
                   "recipe_versions": recipe_versions, **(extra or {})}, f, ensure_ascii=False)
    os.replace(tmp, _path())


def _compute() -> None:
    started = time.time()  # changes during the run make the result stale again
    if target.is_mealie():  # the tiles that work with Mealie (mealie_maintenance)
        try:
            with mealie_client.get_client() as client:
                metrics, item_lists, versions, extra = mealie_maintenance.compute_full(client)
            _write(started, metrics, item_lists, versions, extra)
        except Exception as exc:  # noqa: BLE001
            log.exception("Health overview (Mealie) failed")
            _state["error"] = str(exc)
        finally:
            _state["running"] = False
        return
    try:
        timings = {}
        with tandoor_client.get_client() as client:
            # everything read once, the independent lists at the same time
            read = _read_parallel(timings, {
                "recipes": lambda: fetch_all_recipes_full(client),
                "foods": lambda: tools_ingredients.fetch_all_foods_full(client),
                "categories": lambda: tools_ingredients.fetch_supermarket_categories(client),
                "units": lambda: tools_conversions._fetch_all(client, "unit"),
                "keywords": lambda: tools_conversions._fetch_all(client, "keyword"),
                "conversions": lambda: tools_conversions._fetch_all(client, tools_conversions.CONVERSION_ENDPOINT),
                "books": lambda: migration._tandoor_books(client),  # recipe id -> its cookbooks, for the source tag
            })
            recipes, units, keywords = read["recipes"], read["units"], read["keywords"]
            unit_conversions, books, categories = read["conversions"], read["books"], read["categories"]
            foods = {f["id"]: f for f in read["foods"]}
            food_names = tools_tags.food_name_set(client, read["foods"])
            used_foods = {
                (ing.get("food") or {}).get("id")
                for r in recipes for step in r.get("steps", []) for ing in step.get("ingredients", [])
            } - {None}
            cook_today.save_index(recipes)  # "what can I cook today?" reuses this full read
            source_index.rebuild((r.get("source_url"), r.get("name", "")) for r in recipes)  # duplicates by address
            began = time.monotonic()
            weak_photos = photo_quality.problems_of(
                {str(r["id"]): r["image"] for r in recipes if r.get("image")}, photo_quality.fetcher(client))
            timings["photos"] = time.monotonic() - began
            began = time.monotonic()
            pairs = tools_conversions.recipe_pairs(recipes)
            general, to_estimate = tools_conversions.find_missing(
                client, pairs, respect_ignored=False,
                lists={"foods": read["foods"], "units": units, "conversions": unit_conversions})
            timings["conversions check"] = time.monotonic() - began

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
            "recipes_amounts_missing": recipe_items(recipe_amounts.needs_amounts),
            "recipes_without_season": recipe_items(lambda r: not tools_tags.has_season_tag(r)),
            "recipes_without_source": recipe_items(
                lambda r, store=source_tag.known(): source_tag.missing(
                    r.get("source_url") or "", books.get(r["id"], []),
                    [k["name"] for k in r.get("keywords") or []], store) is not None),
            "recipes_without_servings": recipe_items(tools_recipe_details.lacks_servings),
            "recipes_without_image": recipe_items(tools_recipe_details.lacks_image),
            "recipes_weak_image": [{**i, "name": f"{i['name']} ({photo_quality.describe(weak_photos[i['key']])})"}
                                   for i in recipe_items(lambda r: str(r["id"]) in weak_photos)],
            "recipes_few_tags": recipe_items(
                lambda r: sum(1 for kw in r.get("keywords", []) if kw["name"].strip().casefold() not in food_names)
                < tools_tags.MIN_TAGS_DEFAULT
            ),
        }
        metrics = {"recipes_total": len(recipes), "foods_used": len(used_foods)}
        # lets the recipe tools read only what changed since (recipe_scope.py)
        recipe_versions = {str(r["id"]): r.get("updated_at") for r in recipes}
        # the conversions tool starts from these instead of reading every recipe
        _write(started, metrics, item_lists, recipe_versions,
               {"recipe_pairs": [[f, u, n] for (f, u), n in pairs.items()]})
        log.info("Collection overview: %s, total %.1f s",
                 ", ".join(f"{name} {seconds:.1f} s" for name, seconds in timings.items()), time.time() - started)
    except Exception as exc:  # noqa: BLE001
        log.exception("Health overview failed")
        _state["error"] = str(exc)
    finally:
        _state["running"] = False


# ---------- after a suggestion was applied: the tiles, not a new scan ----------
#
# Every applied suggestion used to make the overview stale - the next visit
# to Maintain then read the whole collection again. What a suggestion fixed
# is known, though: its entries are taken off the tiles right away. Only
# what can't be told that way (unit conversions, undo) still marks it stale.

RECIPE_KINDS = {
    "restructure_recipe": "recipes_need_restructure", "translate_recipe": "recipes_not_translated",
    "amounts_in_steps": "recipes_amounts_missing",
    "season": "recipes_without_season", "suggest_tags": "recipes_few_tags", "source_tag": "recipes_without_source",
    "set_servings": "recipes_without_servings", "generate_image": "recipes_without_image",
    "enhance_image": "recipes_weak_image",
}
ENTITY_METRICS = {
    "food": ("foods_duplicates", "foods_without_nutrition", "foods_without_category", "foods_unused"),
    "unit": ("units_duplicates", "units_unused"),
    "keyword": ("keywords_unused", "keywords_ungrouped"),
}
TOOL_ENTITY = {"ingredients_review": "food", "units_review": "unit", "tags_cleanup": "keyword",
               "tags_simplify": "keyword", "tags_translate": "keyword"}
_settle_lock = threading.Lock()


def _drops(tool: str, suggestion) -> dict | None:
    """{metric: keys to take off} for an applied suggestion - None when it
    can't be told (the overview then counts as stale)."""
    d, kind = suggestion.detail or {}, suggestion.kind
    if kind in RECIPE_KINDS:
        ids = d.get("recipe_ids") or ([d["recipe_id"]] if "recipe_id" in d else [])
        return {RECIPE_KINDS[kind]: {str(i) for i in ids}}
    if kind == "meal_plan":
        return {}
    if kind == "enrich":
        out = {}
        if d.get("nutrition"):
            out["foods_without_nutrition"] = {str(d["food_id"])}
        if d.get("category"):
            out["foods_without_category"] = {str(d["food_id"])}
        return out
    if kind == "food_category":
        return {"foods_without_category": {str(i) for i in d.get("food_ids") or []}}
    if kind == "group_tags":
        return {"keywords_ungrouped": {str(t.get("id") if isinstance(t, dict) else t) for t in d.get("tags") or []}}
    if kind == "delete_unused":
        return _gone(d.get("endpoint"), [d.get("entity_id")])
    if kind == "merge":
        entity = d.get("entity") or TOOL_ENTITY.get(tool)
        removed = list(d.get("remove_ids") or [])
        merged = {str(i) for i in [d.get("keep_id"), *removed] if i is not None}
        out = _gone(entity, removed)
        if out is None:
            return None
        pairs = f"{entity}s_duplicates" if entity in ("food", "unit") else None
        if pairs:  # the pairs inside the merged group are settled too
            out.setdefault(pairs, set())
            out[pairs] |= {("pair", k) for k in merged}
        return out
    return None


def _gone(entity, ids) -> dict | None:
    if entity not in ENTITY_METRICS:
        return None
    keys = {str(i) for i in ids if i is not None}
    return {metric: {("any", k) for k in keys} if metric.endswith("_duplicates") else set(keys)
            for metric in ENTITY_METRICS[entity]}


def _settled(metric: str, key: str, drops: set) -> bool:
    if key in drops:
        return True
    if metric.endswith("_duplicates"):  # key "a:b"
        a_b = set(key.split(":"))
        merged = {k for tag, k in (d for d in drops if isinstance(d, tuple)) if tag == "pair"}
        gone = {k for tag, k in (d for d in drops if isinstance(d, tuple)) if tag == "any"}
        return bool(a_b & gone) or (len(a_b) == 2 and a_b <= merged)
    return False


def settle(tool: str, suggestion) -> None:
    """Takes what an applied suggestion fixed off the tiles - or marks the
    overview stale when that can't be told."""
    if suggestion.kind in ("merge", "rename", "set_plural", "delete_unused", "enrich", "food_category", "group_tags"):
        # ingredients, units or tags changed inside recipes without the
        # recipe manager stamping them - the recipes using them are read again
        from . import recipe_cache
        changed = recipe_cache.changed_entities(tool, suggestion)
        if changed is None:
            recipe_cache.drop()
        else:
            recipe_cache.forget_using(*changed)
    drops = _drops(tool, suggestion)
    if drops is None:
        mark_changed()
        return
    if not drops:
        return
    with _settle_lock:
        data = _read()
        items = data.get("items")
        if not items:
            return
        for metric, keys in drops.items():
            if metric in items:
                items[metric] = [i for i in items[metric] if not _settled(metric, str(i["key"]), keys)]
        _write(data.get("computed_at"), data.get("metrics") or {}, items, data.get("recipe_versions"),
               {k: v for k, v in data.items() if k not in ("computed_at", "metrics", "items", "recipe_versions")})
