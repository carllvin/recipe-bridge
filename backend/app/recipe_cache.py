"""The collection read once in full, then only what changed.

Every full read of the collection (the Maintain overview, tools without an
overview) used to fetch every recipe again. Full recipes are kept in the
data folder with their version (Tandoor "updated_at", Mealie "updatedAt");
a recipe the recipe manager reports unchanged is taken from there.

Changes the recipe manager doesn't stamp on the recipe (an ingredient
renamed or merged, a tag moved into a group) are why an applied change to
ingredients, units or tags forgets the recipes using them - only those are
read again - and why once a day everything is read fresh."""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from .config import settings

log = logging.getLogger("recipe-bridge")

MAX_AGE = 24 * 3600
FILES = {"tandoor": "recipe_cache.json", "mealie": "mealie_recipe_cache.json"}
_lock = threading.Lock()


def _path(kind: str) -> str:
    return os.path.join(settings.data_dir, FILES[kind])


def drop() -> None:
    """Everything is read again next time (e.g. after an undo)."""
    with _lock:
        for kind in FILES:
            try:
                os.remove(_path(kind))
            except OSError:
                pass


def _load(kind: str) -> dict:
    try:
        with open(_path(kind), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if time.time() - (data.get("created_at") or 0) > MAX_AGE:
        return {}
    return data


def _save(kind: str, recipes: dict, created_at: float) -> None:
    with _lock:
        tmp = _path(kind) + ".tmp"
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"created_at": created_at, "recipes": recipes}, f, ensure_ascii=False)
        os.replace(tmp, _path(kind))


def read(kind: str, overview: list[dict], key, version, fetch) -> list[dict]:
    """Every recipe of the overview in full, in its order: the unchanged
    ones from the copy, the others through fetch(list of overview entries)
    -> full recipes. key/version: the overview entry's (or full recipe's)
    id and version."""
    cache = _load(kind)
    kept = cache.get("recipes") or {}

    def unchanged(item):
        old = kept.get(str(key(item)))
        return old is not None and version(item) and version(old) == version(item)
    todo = [item for item in overview if not unchanged(item)]
    fresh = {str(key(r)): r for r in fetch(todo)}
    recipes = [fresh.get(str(key(item))) or kept.get(str(key(item))) for item in overview]
    recipes = [r for r in recipes if r is not None]
    log.info("Collection read: %d recipe(s) fetched, %d unchanged from the last read", len(fresh), len(recipes) - len(fresh))
    _save(kind, {str(key(r)): r for r in recipes}, cache.get("created_at") or time.time())
    return recipes


# ---------- forgetting only the recipes a change touched ----------

def _ids_in(recipe: dict, entity: str) -> set:
    """The ids of the ingredients ("food"), units or tags ("keyword") a
    recipe uses - Tandoor (steps -> ingredients, keywords) or Mealie
    (recipeIngredient, tags)."""
    rows = [ing for step in recipe.get("steps") or [] for ing in step.get("ingredients") or []]
    rows += recipe.get("recipeIngredient") or []
    if entity == "keyword":
        refs = (recipe.get("keywords") or []) + (recipe.get("tags") or [])
    else:
        refs = [row.get(entity) for row in rows]
    return {str(ref.get("id")) for ref in refs if isinstance(ref, dict) and ref.get("id") is not None}


def forget_using(entity: str, ids) -> int:
    """Takes the recipes using these ingredients/units/tags out of the
    copies - they are read again next time, the rest stays. Returns how
    many were forgotten."""
    ids = {str(i) for i in ids if i is not None}
    if not ids:
        return 0
    forgotten = 0
    for kind in FILES:
        cache = _load(kind)
        kept = cache.get("recipes") or {}
        left = {k: r for k, r in kept.items() if not (_ids_in(r, entity) & ids)}
        if len(left) != len(kept):
            forgotten += len(kept) - len(left)
            _save(kind, left, cache["created_at"])
    return forgotten


def changed_entities(tool: str, suggestion) -> tuple[str, set] | None:
    """(entity, ids) an applied suggestion changed inside recipes - None
    when it can't be told (then everything is read again)."""
    d, kind = suggestion.detail or {}, suggestion.kind
    tool_entity = {"ingredients_review": "food", "ingredients_enrich": "food", "units_review": "unit",
                   "tags_cleanup": "keyword", "tags_simplify": "keyword", "tags_translate": "keyword",
                   "tags_groups": "keyword"}.get(tool)
    if kind == "merge":
        entity = d.get("entity") or tool_entity
        return (entity, {d.get("keep_id"), *(d.get("remove_ids") or [])}) if entity else None
    if kind == "rename":
        entity = d.get("entity") or tool_entity
        return (entity, {d.get("id")}) if entity and d.get("id") is not None else None
    if kind == "set_plural":
        return (tool_entity, {d.get("id")}) if tool_entity and d.get("id") is not None else None
    if kind == "enrich":
        return "food", {d.get("food_id")}
    if kind == "food_category":
        return "food", set(d.get("food_ids") or [])
    if kind == "group_tags":
        return "keyword", {t.get("id") if isinstance(t, dict) else t for t in d.get("tags") or []}
    if kind == "delete_unused":
        return d.get("endpoint") or tool_entity or "food", set()  # used by no recipe
    return None
