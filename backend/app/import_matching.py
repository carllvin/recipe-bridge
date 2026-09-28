"""Matches the ingredients, units and tags of freshly extracted recipes
against what already exists in Tandoor, BEFORE import - so importing a
cookbook reuses "Zwiebel" instead of creating a new "Zwiebeln" next to it,
and the review shows everything; nothing is left for the Review page after
an import (see tools_new_recipes: only the ingredient data is filled in).

Same approach as the new-recipes workflow, minus the normalize step (the
extraction already wrote names in OUTPUT_LANGUAGE):
1. exact match (case-insensitive) against existing names and plurals -
   decided in code, the ingredient gets the existing entry's exact name;
2. only names with a merely similar existing name go to the AI, with at
   most a few candidates each (one small call for the whole cookbook);
3. everything else is marked as new.
Each ingredient records the outcome in `tandoor_match` ("exists" |
"matched" | "new") and, when its name was changed, `original_name` (units:
`unit_match` / `original_unit`, tags: the recipe's `tag_status` /
`tag_original`), so the review screen can show it and undo it."""
from __future__ import annotations

import logging

from . import target, tools_tags
from .tools_new_recipes import PICK_SYSTEM_PROMPT, _key, _prompt, _similar_candidates

log = logging.getLogger("tandoor-helper")

PICK_BATCH_SIZE = 60  # names per AI call when a big cookbook has many "similar" ones


def _index(items):
    index = {}
    for item in items:
        for name in (item.get("name"), item.get("plural_name")):
            if name:
                index.setdefault(_key(name), item)
    return index


def _decide(job, entity, names, items) -> dict[str, tuple[str, str | None]]:
    """name key -> ("exists" | "matched" | "new", existing name): exact
    matches in code, merely similar names decided by the AI (batched)."""
    index = _index(items)
    decisions: dict[str, tuple[str, str | None]] = {}
    undecided: dict[str, tuple[str, list[dict]]] = {}  # key -> (name, similar existing items)
    for name in names:
        key = _key(name)
        if not key or key in decisions or key in undecided:
            continue
        if key in index:
            decisions[key] = ("exists", index[key]["name"])
            continue
        similar = _similar_candidates([name], index, own_id=None)
        if similar:
            undecided[key] = (name, similar)
        else:
            decisions[key] = ("new", None)

    entries = list(undecided.items())
    picked = {}
    for start in range(0, len(entries), PICK_BATCH_SIZE):
        batch = list(enumerate(entries))[start:start + PICK_BATCH_SIZE]
        try:
            picks = tools_tags._complete_json(
                job, _prompt(PICK_SYSTEM_PROMPT, entity),
                [{"id": i, "name": name, "candidates": [s["name"] for s in similar]}
                 for i, (_key_, (name, similar)) in batch],
                max_tokens=40 * len(batch) + 100,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("%s matching AI call failed, treating those as new: %s", entity, exc)
            picks = []
        picked.update({p.get("id"): p.get("match_name") for p in picks if isinstance(p, dict)})
    for i, (key, (name, similar)) in enumerate(entries):
        choice = picked.get(i)
        match = next((s for s in similar if isinstance(choice, str) and _key(s["name"]) == _key(choice)), None)
        decisions[key] = ("matched", match["name"]) if match else ("new", None)
    return decisions


def _outcome(decisions, name) -> tuple[str | None, str]:
    """(match, name to use) - a differently spelled existing entry counts as
    "matched", so the review can show it and undo it."""
    decision = decisions.get(_key(name))
    if decision is None:
        return None, name
    match, existing = decision
    if existing and existing != name:
        return "matched", existing
    return match, name


def match_job_ingredients(job) -> None:
    """Updates job.recipes' ingredients, units and tags in place. Non-fatal:
    if Tandoor isn't reachable, the review screen simply shows no badges."""
    try:
        api = target.client()  # Tandoor or Mealie
        with api.get_client() as client:
            foods = target.fetch_foods(client)
            units = api.fetch_all_items(client, "unit")
            # tag groups ("Diet") hold tags - they aren't put on recipes
            keywords = [k for k in api.fetch_all_items(client, "keyword") if not k.get("numchild")]
    except Exception as exc:  # noqa: BLE001
        log.info("Ingredient matching skipped (Tandoor unreachable/not configured): %s", exc)
        return

    all_ingredients = [ing for recipe in job.recipes for ing in recipe.ingredients]

    food_decisions = _decide(job, "food", [ing.name for ing in all_ingredients], foods)
    unit_decisions = _decide(job, "unit", [ing.unit for ing in all_ingredients if ing.unit], units)
    tag_decisions = _decide(job, "keyword", [tag for recipe in job.recipes for tag in recipe.tags], keywords)

    counts = {"exists": 0, "matched": 0, "new": 0}
    for ing in all_ingredients:
        match, name = _outcome(food_decisions, ing.name)
        if match:
            if name != ing.name:
                ing.original_name, ing.name = ing.name, name
            ing.tandoor_match = match
            counts[match] += 1
        if ing.unit:
            match, unit = _outcome(unit_decisions, ing.unit)
            if match:
                if unit != ing.unit:
                    ing.original_unit, ing.unit = ing.unit, unit
                ing.unit_match = match

    for recipe in job.recipes:
        tags, status, original = [], {}, {}
        for tag in recipe.tags:
            match, name = _outcome(tag_decisions, tag)
            if _key(name) in {_key(t) for t in tags}:
                continue  # two tags matched to the same existing one
            tags.append(name)
            status[name] = match or "new"
            if name != tag:
                original[name] = tag
        recipe.tags, recipe.tag_status, recipe.tag_original = tags, status, original
    log.info("Ingredient matching for job %s: %s", job.id, counts)
