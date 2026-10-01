"""Workflow for recipes added since the last run - whether imported through
this app or added in Tandoor itself (URL import, app, by hand).

Which recipes were already handled is remembered in a small JSON file in the
data volume. The first time it's needed, every recipe that exists at that
moment is recorded as handled (the "baseline"), so only recipes added
afterwards are picked up.

Per run, for the new recipes only:
1. Translate recipe text into OUTPUT_LANGUAGE - applied automatically.
   Then, where needed (decided locally): revise the content - split a long
   method into steps, assign ingredients to the steps using them, fill in
   servings/times (reviewed like the rest). And the amounts into the steps
   (recipe_amounts) - for a recipe that gets revised, once the revision is
   applied.
2. Match the units, ingredients and tags these recipes introduced against
   the existing ones (translate / merge into an existing entry).
3. Fill in plural, nutrition and supermarket category for new ingredients,
   and the gram conversions Tandoor needs to calculate their nutrition.
4. Suggest a season tag and further tags.
Steps 2-4 become suggestions that are applied after review. The recipes are
recorded as handled once every suggestion is applied or skipped - so if the
container restarts before that, they simply show up again next time.

After an import through this app the review already covered translation,
structure, matching (import_matching) and tags (added while reading the
recipe) - only step 3 runs, and it is applied right away (_apply_all) - and the
amounts into the steps, which wait for review as they change the text."""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import threading
import time
import uuid

from . import llm_provider, nutrition_properties, recipe_amounts, recipe_restructure, tandoor_client, target, tool_jobs, tools_conversions, tools_ingredients, tools_recipes, tools_tags, tools_units, undo, usage_log
from .config import get_language_code, settings
from .schemas import ToolSuggestion
from .tandoor_helpers import find_recipes_by_filter, resolve_name_collisions

log = logging.getLogger("recipe-bridge")

_store_lock = threading.Lock()


# ---------- Which recipes were already handled ----------

def _store_path() -> str:
    # Mealie's recipes have other ids (slugs) - its own list
    return os.path.join(settings.data_dir, "processed_recipes_mealie.json" if target.is_mealie() else "processed_recipes.json")


def _load_store() -> dict | None:
    try:
        with open(_store_path(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _save_store(store: dict) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    tmp = _store_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f)
    os.replace(tmp, _store_path())  # atomic - never leaves a half-written file


def mark_processed(recipe_ids) -> None:
    with _store_lock:
        store = _load_store() or {"baseline_at": time.time(), "recipe_ids": []}
        store["recipe_ids"] = sorted(set(store["recipe_ids"]) | set(recipe_ids), key=str)
        _save_store(store)


def list_recipe_ids(client) -> list:
    if target.is_mealie():
        from .mealie_tools import list_recipe_ids as mealie_ids
        return mealie_ids(client)
    ids = []
    url, params = "/recipe/", {"page_size": 200}
    for _ in range(200):
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        ids.extend(r["id"] for r in results if r.get("id") is not None)
        url = data.get("next") if isinstance(data, dict) else None
        if not url:
            break
        params = None
    return ids


def open_jobs(exclude_id: str | None = None) -> list:
    """New-recipes runs that are still scanning, or finished but with
    suggestions not yet applied/skipped - their recipes aren't recorded as
    handled yet, but must not be picked up by a second run either (that
    would produce the same suggestions twice)."""
    return [
        job for job in tool_jobs.list_tool_jobs("new_recipes")
        if job.id != exclude_id and not job.meta.get("marked")
        and (job.status == "scanning" or (job.status == "ready" and any(s.status == "pending" for s in job.suggestions)))
    ]


def _in_open_jobs(exclude_id: str | None = None) -> set[int]:
    return {rid for job in open_jobs(exclude_id) for rid in job.meta.get("recipe_ids", [])}


def status() -> dict:
    """How many recipes are new (not handled and not already part of an
    open run), plus the open runs and the automatic-run schedule. Before the
    first run the user decides whether the existing recipes count as handled
    (needs_choice, see set_baseline)."""
    with target.client().get_client() as client:
        ids = list_recipe_ids(client)
    result = {
        "auto_interval_hours": settings.auto_process_interval_hours,
        "next_auto_run_at": _next_auto_run_at,
        "open_jobs": [
            {"id": job.id, "created_at": job.created_at, "status": job.status, "auto": bool(job.meta.get("auto")),
             "pending": sum(1 for s in job.suggestions if s.status == "pending")}
            for job in sorted(open_jobs(), key=lambda j: j.created_at)
        ],
    }
    with _store_lock:
        store = _load_store()
    if store is None:
        return {**result, "new_count": 0, "needs_choice": True, "existing_count": len(ids)}
    new_ids = set(ids) - set(store["recipe_ids"]) - _in_open_jobs()
    return {**result, "new_count": len(new_ids), "needs_choice": False, "existing_count": len(ids),
            "handled_count": len(set(ids) & set(store["recipe_ids"]))}


def set_baseline(existing_done: bool) -> dict:
    """The starting point: every recipe existing now counts as handled
    (existing_done) - or none does, so the next run processes the whole
    collection. Can be changed later; recipes in an open run stay in it."""
    with target.client().get_client() as client:
        ids = list_recipe_ids(client) if existing_done else []
    with _store_lock:
        _save_store({"baseline_at": time.time(), "recipe_ids": sorted(ids, key=str)})
    return status()


def _new_recipe_ids(client, job_id: str | None = None) -> list[int]:
    store = _load_store()
    if store is None:
        raise tandoor_client.TandoorError("Choose first whether the existing recipes count as handled (Maintain → Process new recipes).")
    skip = set(store["recipe_ids"]) | _in_open_jobs(exclude_id=job_id)
    return [rid for rid in list_recipe_ids(client) if rid not in skip]


# ---------- Automatic runs ----------

_next_auto_run_at: float | None = None


def auto_run_once() -> str | None:
    """Starts a new-recipes run if there's anything new (blocking - call from
    a worker thread). Returns the job id, or None if nothing was started.
    Never creates the baseline itself: until someone has opened the tools
    page once, there's no "since" to compare against."""
    if not llm_provider.is_configured() or _load_store() is None:
        return None
    if not usage_log.automatic_runs_allowed():
        log.info("Automatic new-recipes run skipped - monthly AI budget reached")
        return None
    if any(job.status == "scanning" for job in open_jobs()):
        return None  # one run at a time
    with target.client().get_client() as client:
        if not _new_recipe_ids(client):
            return None
    job = tool_jobs.create_tool_job("new_recipes")
    job.meta["auto"] = True
    if target.is_mealie():
        job.meta["target"] = "mealie"  # applied by mealie_maintenance
    tool_jobs.save_tool_job(job)
    log.info("Automatic new-recipes run started (job %s)", job.id)
    run_scan(job.id)
    return job.id


def start_after_import(imported_recipe_ids) -> str | None:
    """Called right after recipes were imported through this app: starts a
    new-recipes run in the background (non-blocking) and returns its job id,
    or None if nothing was started. If there's no baseline yet, it's created
    now - from every recipe EXCEPT the ones just imported, so those still
    count as new."""
    if not imported_recipe_ids or not llm_provider.is_configured():
        return None
    if not usage_log.automatic_runs_allowed():
        log.info("Post-processing after import skipped - monthly AI budget reached")
        return None
    imported = set(imported_recipe_ids)
    with _store_lock:
        if _load_store() is None:
            with tandoor_client.get_client() as client:
                existing = [rid for rid in list_recipe_ids(client) if rid not in imported]
            _save_store({"baseline_at": time.time(), "recipe_ids": sorted(existing)})
    job = tool_jobs.create_tool_job("new_recipes")
    job.meta["auto"] = True
    job.meta["trigger"] = "import"
    tool_jobs.save_tool_job(job)
    log.info("New-recipes run started after import (job %s)", job.id)
    threading.Thread(target=run_scan, args=(job.id,), daemon=True).start()
    return job.id


async def auto_run_loop() -> None:
    """Background loop (started by main.py when AUTO_PROCESS_INTERVAL_HOURS > 0):
    every N hours, process recipes added since the last run. Translations are
    applied right away; everything else waits in the tools page for review."""
    global _next_auto_run_at
    interval = settings.auto_process_interval_hours * 3600
    _next_auto_run_at = time.time() + 60
    await asyncio.sleep(60)  # let the app finish starting up first
    while True:
        try:
            await asyncio.to_thread(auto_run_once)
        except Exception:  # noqa: BLE001
            log.exception("Automatic new-recipes run failed")
        _next_auto_run_at = time.time() + interval
        await asyncio.sleep(interval)


# ---------- Matching new units/ingredients/tags against existing ones ----------

NORMALIZE_SYSTEM_PROMPT = """You clean up {entity} names in a home cook's
database. The target language is {language}. You will receive a JSON array
of {"id": integer, "name": string}.

For each entry answer the standard {language} name a {language} cookbook
would use ({style}) - translate it if it's in another language, e.g.
{variants}. Also list up to 3 other common {language} names for exactly the
same thing (regional names, synonyms), if there are any.

Respond with ONLY a JSON array (no explanation, no markdown fence), one
element per entry: {"id": <id>, "name": <standard name>, "alternatives": [<other names>]}
"""

PICK_SYSTEM_PROMPT = """You match {entity} entries in a home cook's database.
The target language is {language}. You will receive a JSON array of
{"id": integer, "name": string, "candidates": [string, ...]}.

For each entry: if it means EXACTLY the same {entity_singular} as one of its
candidates (spelling or singular/plural variant, synonym), answer that
candidate's name exactly as listed. If it's merely similar or related (e.g.
"Frühlingszwiebel" vs "Zwiebel", "Vollmilch" vs "Milch"), answer null.

Respond with ONLY a JSON array (no explanation, no markdown fence), one
element per entry: {"id": <id>, "match_name": <candidate name or null>}
"""

ENTITY_RULES = {
    "food": dict(entity="ingredient", entity_singular="ingredient",
                 variants='"onions" -> "Zwiebel", "gehackte Petersilie" -> "Petersilie"',
                 style="a plain, singular base noun, not a prepared form"),
    "unit": dict(entity="unit-of-measure", entity_singular="unit",
                 variants='"tbsp" / "Esslöffel" -> "EL", "grams" -> "g"',
                 style="the conventional short form used in recipes, e.g. g, ml, EL, TL, Stück"),
    "keyword": dict(entity="recipe tag", entity_singular="tag",
                    variants='"cakes" -> "Kuchen", "quick" -> "Schnell"',
                    style="a short word or phrase in recipe-tag style"),
}
ENTITY_LABEL = {"food": "ingredient", "unit": "unit", "keyword": "tag"}
MAX_CANDIDATES = 5


def _prompt(template, entity):
    for key, value in {**ENTITY_RULES[entity], "language": settings.output_language}.items():
        template = template.replace("{" + key + "}", value)
    return template


def _key(name) -> str:
    return " ".join((name or "").lower().split())


def _similar_candidates(names, index, own_id) -> list[dict]:
    """Existing items whose name looks close to any of `names` (fuzzy match
    or one contained in the other), best first - for the AI to decide on."""
    scored = {}
    for name in names:
        key = _key(name)
        if not key:
            continue
        for other_key, item in index.items():
            if item["id"] == own_id:
                continue
            contained = min(len(key), len(other_key)) >= 4 and (key in other_key or other_key in key)
            matcher = difflib.SequenceMatcher(None, key, other_key)
            if not contained and matcher.quick_ratio() < 0.75:
                continue  # cheap upper bound - skips the full ratio for most pairs
            ratio = matcher.ratio()
            if ratio >= 0.75 or contained:
                scored[item["id"]] = max(scored.get(item["id"], (0, item))[0], ratio), item
    ranked = sorted(scored.values(), key=lambda pair: -pair[0])
    return [item for _ratio, item in ranked[:MAX_CANDIDATES]]


def _match_actions(job, entity, candidates, all_items) -> list[dict]:
    """Matches `candidates` against the existing items WITHOUT sending the
    whole existing list to the AI:
    1. AI: normalize only the candidates' names (standard name + synonyms).
    2. Code: exact match of those against existing names/plurals -> merge.
    3. AI again, only for the few with a merely similar existing name, with
       at most MAX_CANDIDATES names each -> merge or not.
    Anything unmatched whose name changed becomes a rename. Returns
    collision-resolved, consolidated actions."""
    if not candidates:
        return []
    candidate_ids = {c["id"] for c in candidates}
    normalized = tools_tags._complete_json(
        job, _prompt(NORMALIZE_SYSTEM_PROMPT, entity),
        [{"id": c["id"], "name": c["name"]} for c in candidates],
        max_tokens=50 * len(candidates) + 200,
    )
    normalized = {
        n["id"]: n for n in normalized if isinstance(n, dict) and n.get("id") in candidate_ids
        and isinstance(n.get("name"), str) and n["name"].strip()
    }

    # Index of existing names (and plurals, where known) -> item.
    index = {}
    for item in all_items:
        for name in (item.get("name"), item.get("plural_name")):
            if name:
                index.setdefault(_key(name), item)

    actions, undecided = [], []
    for cand in candidates:
        norm = normalized.get(cand["id"])
        if norm is None:
            continue
        clean = norm["name"].strip()
        names = [clean, cand["name"]] + [a for a in norm.get("alternatives") or [] if isinstance(a, str)]
        match = next((index[_key(n)] for n in names if _key(n) in index and index[_key(n)]["id"] != cand["id"]), None)
        if match:
            actions.append({"type": "merge", "keep_id": match["id"], "keep_name": match["name"], "remove_ids": [cand["id"]]})
            continue
        similar = _similar_candidates(names, index, cand["id"])
        if similar:
            undecided.append((cand, clean, similar))
        elif clean != cand["name"]:
            actions.append({"type": "rename", "id": cand["id"], "new_name": clean})

    if undecided:
        by_id = {cand["id"]: (cand, clean, similar) for cand, clean, similar in undecided}
        try:
            picks = tools_tags._complete_json(
                job, _prompt(PICK_SYSTEM_PROMPT, entity),
                [{"id": cand["id"], "name": clean, "candidates": [s["name"] for s in similar]}
                 for cand, clean, similar in undecided],
                max_tokens=40 * len(undecided) + 100,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Matching %s candidates failed: %s", entity, exc)
            picks = []
        picked = {p["id"]: p.get("match_name") for p in picks if isinstance(p, dict) and p.get("id") in by_id}
        for cand_id, (cand, clean, similar) in by_id.items():
            match = next((s for s in similar if isinstance(picked.get(cand_id), str)
                          and _key(s["name"]) == _key(picked[cand_id])), None)
            if match:
                actions.append({"type": "merge", "keep_id": match["id"], "keep_name": match["name"], "remove_ids": [cand_id]})
            elif clean != cand["name"]:
                actions.append({"type": "rename", "id": cand_id, "new_name": clean})

    return resolve_name_collisions(actions, all_items)


def _describe(entity, action, by_id):
    label = ENTITY_LABEL[entity]
    if entity == "food":
        return tools_ingredients.describe_food_action(action, by_id, f"{label}: ")
    if action["type"] == "rename":
        return f"{label}: rename {by_id.get(action['id'], {}).get('name', '?')!r} -> {action['new_name']!r}"
    removed = ", ".join(repr(by_id.get(rid, {}).get("name", "?")) for rid in action["remove_ids"])
    return f"{label}: merge {removed} into {by_id.get(action['keep_id'], {}).get('name', '?')!r} -> {action['keep_name']!r}"


def _only_used_by(client, filter_param, item_id, recipe_ids) -> bool:
    """True if every recipe using this food/keyword is one of the new ones -
    i.e. the entry was introduced by them, not an established one."""
    return all(r["id"] in recipe_ids for r in find_recipes_by_filter(client, filter_param, item_id))


# ---------- The scan ----------

def run_scan(job_id: str) -> None:
    if target.is_mealie():  # mealie_tools.new_recipes
        from . import mealie_maintenance
        return mealie_maintenance.run_scan(job_id)
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        return
    try:
        if not llm_provider.is_configured():
            job.status = "error"
            job.error = llm_provider.missing_key_hint()
            tool_jobs.save_tool_job(job)
            return

        with tandoor_client.get_client() as client:
            job.progress_label = "Looking for new recipes..."
            tool_jobs.save_tool_job(job)
            new_ids = _new_recipe_ids(client, job_id)
            job.meta["recipe_ids"] = new_ids
            job.progress_total = len(new_ids)
            recipes = []
            for rid in new_ids:
                resp = client.get(f"/recipe/{rid}/")
                if resp.status_code == 200:
                    recipes.append(resp.json())
            job.cost_estimate = (
                f"{len(recipes)} new recipe(s): a few batched AI calls for ingredients, units and tags, plus up to "
                f"two small calls per recipe lacking the amounts in its steps."
                if job.meta.get("trigger") == "import" else
                f"{len(recipes)} new recipe(s): up to one AI call per recipe needing translation and one per "
                f"recipe needing a structural revision, up to two small ones per recipe lacking the amounts in its "
                f"steps, plus a few batched calls for ingredients, units and tags."
            )
            tool_jobs.save_tool_job(job)
            suggestions: list[ToolSuggestion] = []
            # Recipes imported through this app were already translated and
            # structured by the AI while reading them - steps 1 and 1b are only
            # for recipes added in Tandoor itself.
            from_import = job.meta.get("trigger") == "import"

            # 1. Translate - applied right away.
            expected_code = get_language_code(settings.output_language)
            for i, recipe in enumerate(list(recipes)):
                if job.cancel_requested or from_import:
                    break
                if tools_recipes.already_in_target_language(recipe, expected_code):
                    continue
                job.progress_label = f"Translating {recipe.get('name', '')!r}..."
                tool_jobs.save_tool_job(job)
                suggestion = ToolSuggestion(id=uuid.uuid4().hex[:10], kind="translate_recipe",
                                            summary=f"recipe: translate {recipe.get('name', '')!r}")
                try:
                    translated, usage = tools_recipes.translate_recipe_text(recipe, settings.output_language)
                    job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
                    job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
                    suggestion.summary = f"recipe: translated {recipe.get('name', '')!r} -> {translated['title']!r}"
                    suggestion.preview = tools_recipes.describe_changes(recipe, translated)
                    with undo.recording() as journal:  # auto-applied, but undoable like the others
                        resp = client.patch(f"/recipe/{recipe['id']}/", json=tools_recipes.build_update_payload(recipe, translated))
                    if resp.status_code not in (200, 201):
                        raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
                    suggestion.status = "applied"
                    suggestion.applied_at = time.time()
                    if journal:
                        undo.save(job.id, suggestion.id, journal)
                        suggestion.undoable = True
                    fresh = client.get(f"/recipe/{recipe['id']}/")
                    if fresh.status_code == 200:
                        recipes[i] = fresh.json()
                except Exception as exc:  # noqa: BLE001
                    suggestion.status = "error"
                    suggestion.error = f"Translation failed: {exc}"
                suggestions.append(suggestion)
            recipe_ids = {r["id"] for r in recipes}

            # 1b. Content revision - split long methods into steps, assign
            # ingredients to the steps using them, fill servings/times. Only
            # for recipes that need it (decided locally); reviewed, not
            # auto-applied.
            for recipe in recipes:
                if job.cancel_requested or from_import:
                    break
                if not recipe_restructure.needs_restructure(recipe):
                    continue
                job.progress_label = f"Revising {recipe.get('name', '')!r}..."
                tool_jobs.save_tool_job(job)
                suggestion = recipe_restructure.plan_suggestion(job, recipe)
                if suggestion:
                    suggestions.append(suggestion)

            # 1c. Amounts into the steps - reviewed. Recipes that get revised
            # above follow once the revision is applied (apply_suggestion).
            for recipe in recipes:
                if job.cancel_requested:
                    break
                if not recipe_amounts.needs_amounts(recipe):
                    continue
                job.progress_label = f"Amounts into the steps of {recipe.get('name', '')!r}..."
                tool_jobs.save_tool_job(job)
                suggestion = recipe_amounts.plan_suggestion(job, recipe)
                if suggestion:
                    suggestions.append(suggestion)

            # 2. Match units, ingredients and tags against the existing ones.
            removed_food_ids = set()
            # Name each entry will have once the suggestions are applied, so
            # later steps judge the final name (e.g. no plural "Lauch" for an
            # ingredient about to be renamed from "leek" to "Lauch").
            final_names = {"food": {}, "keyword": {}}
            merged_into = {"food": {}, "unit": {}}  # removed id -> kept id, once pending merges are applied
            if not job.cancel_requested:
                job.progress_label = "Comparing units, ingredients and tags with existing ones..."
                tool_jobs.save_tool_job(job)

                used = {"food": {}, "unit": {}, "keyword": {}}
                for recipe in recipes:
                    for kw in recipe.get("keywords", []):
                        used["keyword"][kw["id"]] = kw
                    for step in recipe.get("steps", []):
                        for ing in step.get("ingredients", []):
                            for entity in ("food", "unit"):
                                ref = ing.get(entity)
                                if ref and ref.get("id") is not None:
                                    used[entity][ref["id"]] = ref

                candidates = {} if from_import else {
                    # (after an import the review already matched them)
                    # Units have no recipe filter in Tandoor's API, and the
                    # list is small - so all units of the new recipes go in.
                    "unit": list(used["unit"].values()),
                    "food": [f for f in used["food"].values() if _only_used_by(client, "foods", f["id"], recipe_ids)],
                    "keyword": [k for k in used["keyword"].values()
                                if _only_used_by(client, "keywords", k["id"], recipe_ids)],
                }
                for entity in ("unit", "food", "keyword"):
                    if job.cancel_requested or not candidates.get(entity):
                        continue
                    all_items = (tools_ingredients.fetch_all_foods_full(client) if entity == "food"
                                 else tandoor_client.fetch_all_items(client, entity))
                    by_id = {item["id"]: item for item in all_items}
                    try:
                        actions = _match_actions(job, entity, candidates[entity], all_items)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Matching %s failed: %s", entity, exc)
                        continue
                    for action in actions:
                        if entity == "food" and action["type"] == "merge":
                            removed_food_ids.update(action["remove_ids"])
                        if entity in merged_into and action["type"] == "merge":
                            for removed in action["remove_ids"]:
                                merged_into[entity][removed] = action["keep_id"]
                        if entity in final_names:
                            if action["type"] == "rename":
                                final_names[entity][action["id"]] = action["new_name"]
                            else:
                                for item_id in [action["keep_id"], *action["remove_ids"]]:
                                    final_names[entity][item_id] = action["keep_name"]
                        suggestions.append(ToolSuggestion(
                            id=uuid.uuid4().hex[:10], kind=action["type"],
                            summary=_describe(entity, action, by_id), detail={**action, "entity": entity},
                        ))

                # 3. Plural / nutrition / category for ALL ingredients the new
                # recipes use - also existing ones that never got them - as
                # they'll be once the pending merges are applied.
                keep_food_ids = {merged_into["food"].get(fid, fid) for fid in used["food"]} - removed_food_ids
                if keep_food_ids and not job.cancel_requested:
                    foods = []
                    for food_id in keep_food_ids:
                        resp = client.get(f"/food/{food_id}/")
                        if resp.status_code == 200:
                            food = resp.json()
                            foods.append({**food, "name": final_names["food"].get(food_id, food["name"])})
                    categories = tools_ingredients.fetch_supermarket_categories(client)
                    try:
                        nutrition_available = bool(nutrition_properties.existing_nutrient_types(client))
                    except tandoor_client.TandoorError:
                        nutrition_available = False
                    targets = tools_ingredients.enrich_targets(foods, categories, nutrition_available)
                    suggestions += tools_ingredients.enrich_suggestions(job, targets, categories)

                # 3b. Gram conversions for the new recipes' ingredient lines,
                # so Tandoor can calculate their nutrition - judged as if the
                # pending merges were applied, and counting ingredients that
                # are about to get nutrition values from step 3.
                if not job.cancel_requested:
                    pairs = {}
                    for (food_id, unit_id), count in tools_conversions.recipe_pairs(recipes).items():
                        key = (merged_into["food"].get(food_id, food_id), merged_into["unit"].get(unit_id, unit_id))
                        pairs[key] = pairs.get(key, 0) + count
                    pending_nutrition = {
                        sug.detail["food_id"]: sug.detail["nutrition"]["basis"]
                        for sug in suggestions if sug.kind == "enrich" and sug.detail.get("nutrition")
                    }
                    try:
                        suggestions += tools_conversions.conversion_suggestions(
                            job, client, pairs, pending_nutrition, final_names["food"]
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Conversion suggestions failed: %s", exc)

                # 4. Season + more tags for the new recipes, judged by their
                # tags' final names.
                recipes = [
                    {**r, "keywords": [{**kw, "name": final_names["keyword"].get(kw["id"], kw["name"])}
                                       for kw in r.get("keywords", [])]}
                    for r in recipes
                ]
                if not job.cancel_requested and not from_import:  # tagged while reading the recipe
                    suggestions += tools_tags.season_suggestions(
                        job, [r for r in recipes if not tools_tags.has_season_tag(r)]
                    )
                if not job.cancel_requested and not from_import:
                    all_tags = tandoor_client.fetch_all_items(client, "keyword")
                    food_names = tools_tags.food_name_set(client)
                    suggestions += tools_tags.suggest_tags_suggestions(
                        job, recipes, tools_tags.tag_vocabulary(all_tags, recipes, food_names), food_names
                    )

            job.suggestions = suggestions
            if from_import:
                job.meta["notified"] = True  # applied below - nothing waits for review
            job.status = "cancelled" if job.cancel_requested else "ready"
            job.progress_label = None
            tool_jobs.save_tool_job(job)
            if from_import and job.status == "ready":
                _apply_all(job)
            after_action(job)

    except Exception as exc:  # noqa: BLE001
        log.exception("New-recipes scan failed for job %s", job_id)
        job.status = "error"
        job.error = str(exc)
        tool_jobs.save_tool_job(job)


# main.py hands in its apply function (records undo data and "recently
# applied" like a click in the UI) - see configure().
_apply_fn = None


def configure(apply_fn) -> None:
    global _apply_fn
    _apply_fn = apply_fn


def _apply_all(job) -> None:
    """After an import through this app: the ingredient data (plural,
    nutrition, category, gram conversions) is filled in without review -
    it's data, not a matter of taste, and every change shows under "recently
    applied" with undo. Suggestions that fail stay in Review as failed."""
    if _apply_fn is None:
        return
    job.progress_label = "Filling in the new ingredients..."
    tool_jobs.save_tool_job(job)
    for suggestion in list(job.suggestions):
        if suggestion.status != "pending" or suggestion.kind == "amounts_in_steps":
            continue  # the text changes wait for review
        try:
            _apply_fn(job.id, suggestion.id, "apply")
        except Exception as exc:  # noqa: BLE001
            log.warning("Auto-apply after import failed for %s: %s", suggestion.id, exc)
    job.progress_label = None
    tool_jobs.save_tool_job(job)


def after_action(job) -> None:
    """Records the job's recipes as handled once nothing is left pending
    (called after the scan and after every apply/skip). A cancelled run
    never counts - it may have stopped before looking at everything."""
    if job.tool != "new_recipes" or job.status != "ready" or job.meta.get("marked"):
        return
    if any(s.status == "pending" for s in job.suggestions):
        return
    mark_processed(job.meta.get("recipe_ids", []))
    job.meta["marked"] = True
    tool_jobs.save_tool_job(job)


def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    """Routes each suggestion to the tool that already knows how to apply it."""
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise tandoor_client.TandoorError("Job not found.")
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise tandoor_client.TandoorError("Suggestion not found.")

    entity = suggestion.detail.get("entity")
    if suggestion.kind == "restructure_recipe":
        result = recipe_restructure.apply_plan(job, suggestion)
        if result.status == "applied":
            try:
                with tandoor_client.get_client() as client:
                    resp = client.get(f"/recipe/{suggestion.detail['recipe_id']}/")
                if resp.status_code == 200:
                    recipe_amounts.follow_up(job, resp.json())
            except Exception as exc:  # noqa: BLE001 - the revision itself is done
                log.warning("Amounts after the revision failed: %s", exc)
    elif suggestion.kind == "amounts_in_steps":
        result = recipe_amounts.apply_plan(job, suggestion)
    elif entity == "food":
        result = tools_ingredients.apply_suggestion(job_id, suggestion_id)
    elif entity == "unit":
        result = tools_units.apply_suggestion(job_id, suggestion_id)
    elif suggestion.kind == "enrich":
        result = tools_ingredients.apply_enrich_suggestion(job_id, suggestion_id)
    elif suggestion.kind == "conversion":
        result = tools_conversions.apply_suggestion(job_id, suggestion_id)
    else:  # keyword rename/merge, season, suggest_tags
        result = tools_tags.apply_suggestion(job_id, suggestion_id)
    after_action(job)
    return result
