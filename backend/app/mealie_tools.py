"""The AI maintenance tools with Mealie (RECIPE_MANAGER=mealie): tags
(clean up / simplify / translate, season, more tags), translating recipes,
revising their structure, and the new-recipes run.

They reuse the Tandoor tools' prompts and suggestion logic unchanged:
view() turns a Mealie recipe into the Tandoor shape those read (steps with
their ingredients, keywords, servings, times), and the functions below
write the results back the Mealie way:

- steps are Mealie's instructions; an ingredient belongs to the first step
  that references it (ingredientReferences), unreferenced ones to step 1 -
  so a recipe without references counts as "all ingredients in step 1" and
  the revision assigns them;
- a recipe's id in suggestions is its slug (Mealie's API takes it, and it's
  short enough to hand to the AI); tags, ingredients and units get small
  numbers for the AI and are mapped back to Mealie's uuids.

Changes in Mealie can't be undone from the helper."""
from __future__ import annotations

import logging
import re
import time
import uuid

from . import ignored, llm_provider, mealie_client, recipe_restructure, tool_jobs, tools_new_recipes, tools_recipes, tools_tags
from .config import get_language_code, settings
from .mealie_plan import minutes as _parse_minutes
from .schemas import ToolSuggestion
from .tandoor_client import TandoorError
from .tandoor_helpers import chunked, format_cost_estimate, resolve_name_collisions, validate_actions

log = logging.getLogger("recipe-bridge")

TAG_REVIEW_PROMPTS = {"tags_cleanup": tools_tags.CLEANUP_SYSTEM_PROMPT, "tags_simplify": tools_tags.SIMPLIFY_SYSTEM_PROMPT,
                      "tags_translate": tools_tags.TRANSLATE_SYSTEM_PROMPT}
TOOLS = [*TAG_REVIEW_PROMPTS, "tags_season", "tags_suggest_more", "recipes_translate", "recipes_restructure", "new_recipes"]
METRICS = ["recipes_not_translated", "recipes_need_restructure", "recipes_without_season", "recipes_few_tags"]
PATHS = {"food": "/foods", "unit": "/units", "keyword": "/organizers/tags"}


# ---------- a Mealie recipe in Tandoor's shape ----------

def _time(recipe, *fields) -> int:
    return sum(_parse_minutes({"totalTime": recipe.get(f)}) for f in fields)


def view(recipe: dict, for_tags: bool = False) -> dict:
    """for_tags: ingredient lines without a linked food (not parsed in
    Mealie) show their text as the food name - only for judging tags."""
    instructions = recipe.get("recipeInstructions") or []
    rows = recipe.get("recipeIngredient") or []
    first_step = {}
    for n, step in enumerate(instructions):
        for ref in step.get("ingredientReferences") or []:
            first_step.setdefault(ref.get("referenceId"), n)
    steps = [{"name": s.get("title") or "", "instruction": s.get("text") or "", "ingredients": []} for s in instructions]
    if not steps and rows:
        steps = [{"name": "", "instruction": "", "ingredients": []}]
    for pos, row in enumerate(rows):
        food = row.get("food")
        text = (row.get("note") or row.get("display") or row.get("originalText") or "").strip()
        if for_tags and not food and text:
            food = {"id": None, "name": text}
        ing = {
            "id": row.get("referenceId") or f"pos{pos}",
            "food": {"id": food.get("id"), "name": food.get("name", ""), "plural_name": food.get("pluralName") or ""} if food else None,
            "unit": {"id": row["unit"].get("id"), "name": row["unit"].get("name", "")} if row.get("unit") else None,
            "amount": row.get("quantity") or 0,
            "no_amount": bool(row.get("disableAmount")),
            "note": row.get("note") or "",
        }
        steps[min(first_step.get(row.get("referenceId"), 0), len(steps) - 1)]["ingredients"].append(ing)
    working = _time(recipe, "prepTime")
    waiting = _time(recipe, "performTime", "cookTime")
    return {
        "id": recipe.get("slug"), "slug": recipe.get("slug"), "name": recipe.get("name", ""),
        "description": recipe.get("description") or "",
        "keywords": [{"id": t.get("id"), "name": t.get("name", "")} for t in recipe.get("tags") or []],
        "servings": recipe.get("recipeServings") or recipe.get("recipeYieldQuantity") or 0,
        "working_time": working or (0 if waiting else _time(recipe, "totalTime")),
        "waiting_time": waiting,
        "steps": steps,
    }


def _fetch(client, slug) -> dict:
    return mealie_client._check(client.get(f"/recipes/{slug}"), "loading the recipe").json()


def _patch(client, slug, body) -> None:
    mealie_client._check(client.patch(f"/recipes/{slug}", json=body), "saving the recipe")


def fetch_recipes_full(client) -> list[dict]:
    from .mealie_maintenance import fetch_recipes_full as fetch
    return fetch(client)


def food_names(foods) -> set[str]:
    return {f["name"].strip().casefold() for f in foods if f.get("name")}


def few_tags(recipe_view, names) -> bool:
    return sum(1 for kw in recipe_view["keywords"] if kw["name"].strip().casefold() not in names) < tools_tags.MIN_TAGS_DEFAULT


def metric_items(recipes, foods) -> dict:
    """The health overview's recipe tiles for these tools (keys = slugs)."""
    expected = get_language_code(settings.output_language)
    names = food_names(foods)
    views = [(view(r), view(r, for_tags=True)) for r in sorted(recipes, key=lambda r: (r.get("name") or "").casefold())]

    def items(matches):
        return [{"key": v["slug"], "name": v["name"], "recipe_id": v["slug"]} for v, tv in views if matches(v, tv)]
    return {
        "recipes_not_translated": items(lambda v, tv: not tools_recipes.already_in_target_language(v, expected)),
        "recipes_need_restructure": items(lambda v, tv: bool(recipe_restructure.needs_restructure(v))),
        "recipes_without_season": items(lambda v, tv: not tools_tags.has_season_tag(v)),
        "recipes_few_tags": items(lambda v, tv: few_tags(tv, names)),
    }


# ---------- writing back ----------

def _row_ids(recipe) -> list[str]:
    """The ingredient ids view() gives this recipe's rows, in order."""
    return [row.get("referenceId") or f"pos{pos}" for pos, row in enumerate(recipe.get("recipeIngredient") or [])]


def apply_translation(client, slug, translated, shape) -> None:
    recipe = _fetch(client, slug)
    v = view(recipe)
    if tools_recipes._shape(v) != shape:
        raise TandoorError("The recipe's steps/ingredients changed since the scan - rescan to translate it.")
    notes = translated.get("ingredient_notes") or {}
    new_notes = {}
    for si, step in enumerate(v["steps"]):
        for ii, ing in enumerate(step["ingredients"]):
            if f"{si}.{ii}" in notes:
                new_notes[ing["id"]] = notes[f"{si}.{ii}"]
    rows = []
    for row_id, row in zip(_row_ids(recipe), recipe.get("recipeIngredient") or []):
        rows.append({**row, "note": new_notes[row_id]} if row_id in new_notes else row)
    instructions = []
    for inst, new in zip(recipe.get("recipeInstructions") or [], translated["steps"]):
        inst = {**inst, "text": new.get("instruction") or inst.get("text", "")}
        if inst.get("title"):
            inst["title"] = new.get("title") or inst["title"]
        instructions.append(inst)
    _patch(client, slug, {"name": translated["title"][:mealie_client.NAME_MAX_LENGTH],
                          "description": translated.get("description") or "",
                          "recipeInstructions": instructions + (recipe.get("recipeInstructions") or [])[len(instructions):],
                          "recipeIngredient": rows})


def apply_restructure(client, slug, plan) -> None:
    recipe = _fetch(client, slug)
    v = view(recipe)
    if not recipe_restructure.same_ingredients(v, plan):
        raise TandoorError("The recipe's ingredients changed since the scan - rescan to revise it.")
    # rows without a reference id get one now, so steps can point to them
    ref_of = {}
    rows = []
    for row_id, row in zip(_row_ids(recipe), recipe.get("recipeIngredient") or []):
        row = dict(row)
        row["referenceId"] = row.get("referenceId") or str(uuid.uuid4())
        ref_of[row_id] = row["referenceId"]
        rows.append(row)
    key_to_row = plan["keys"]
    old = recipe.get("recipeInstructions") or []
    instructions, order = [], []
    for n, planned in enumerate(plan["steps"]):
        base = dict(old[n]) if n < len(old) else {"id": str(uuid.uuid4()), "summary": ""}
        refs = [ref_of[key_to_row[k]] for k in planned["ingredients"] if key_to_row.get(k) in ref_of]
        base.update({"title": planned.get("title") or "", "text": planned["instruction"],
                     "ingredientReferences": [{"referenceId": r} for r in refs]})
        instructions.append(base)
        order += refs
    # the ingredient list in the order the steps use them
    position = {ref: n for n, ref in enumerate(order)}
    rows.sort(key=lambda r: position.get(r["referenceId"], len(position)))
    body = {"recipeInstructions": instructions, "recipeIngredient": rows}
    if plan.get("servings") and not v["servings"]:
        body["recipeServings"] = plan["servings"]
    if not v["working_time"] and not v["waiting_time"]:
        if plan.get("working_time"):
            body["prepTime"] = f"{plan['working_time']} min"
        if plan.get("waiting_time"):
            body["performTime"] = f"{plan['waiting_time']} min"
        total = (plan.get("working_time") or 0) + (plan.get("waiting_time") or 0)
        if total and not recipe.get("totalTime"):
            body["totalTime"] = f"{total} min"
    _patch(client, slug, body)


def add_tags(client, slug, names) -> None:
    recipe = _fetch(client, slug)
    tags = [{"id": t["id"], "name": t["name"], "slug": t.get("slug")} for t in recipe.get("tags") or []]
    have = {t["name"].strip().casefold() for t in tags}
    lookup = mealie_client._Lookup(client)
    for name in names:
        if name.strip().casefold() in have:
            continue
        tag = lookup.get_or_create("keyword", name)
        tags.append({"id": tag["id"], "name": tag["name"], "slug": tag.get("slug")})
        have.add(name.strip().casefold())
    _patch(client, slug, {"tags": tags})


def _exists(client, entity, item_id) -> bool:
    return client.get(f"{PATHS[entity]}/{item_id}").status_code == 200


def rename(client, entity, item_id, new_name) -> None:
    if entity == "keyword":
        body = {"name": new_name}
    else:  # foods / units: the whole entry with the new name
        body = {**mealie_client._check(client.get(f"{PATHS[entity]}/{item_id}"), "loading the entry").json(), "name": new_name}
    mealie_client._check(client.put(f"{PATHS[entity]}/{item_id}", json=body), f"renaming to {new_name!r}")


def merge(client, entity, keep_id, keep_name, remove_ids) -> None:
    if not _exists(client, entity, keep_id):
        raise TandoorError("The entry to keep no longer exists (already merged by another suggestion?) - rescan to continue.")
    current = mealie_client._check(client.get(f"{PATHS[entity]}/{keep_id}"), "loading the entry").json()
    if current.get("name") != keep_name:
        rename(client, entity, keep_id, keep_name)
    for remove_id in remove_ids:
        if not _exists(client, entity, remove_id):
            continue  # merged away by an earlier suggestion
        if entity == "food":
            mealie_client._check(client.put("/foods/merge", json={"fromFood": remove_id, "toFood": keep_id}), "the merge")
        elif entity == "unit":
            mealie_client._check(client.put("/units/merge", json={"fromUnit": remove_id, "toUnit": keep_id}), "the merge")
        else:  # tags: Mealie has no merge - put the kept tag on the recipes, then delete the other
            keep = {"id": keep_id, "name": keep_name, "slug": current.get("slug")}
            for recipe in mealie_client._paged(client, "/recipes"):
                ids = [t.get("id") for t in recipe.get("tags") or []]
                if remove_id not in ids:
                    continue
                tags = [t for t in recipe["tags"] if t.get("id") not in (remove_id, keep_id)] + [keep]
                _patch(client, recipe["slug"], {"tags": [{"id": t["id"], "name": t["name"], "slug": t.get("slug")} for t in tags]})
            mealie_client._check(client.delete(f"{PATHS[entity]}/{remove_id}"), "deleting the merged tag")


KINDS = {"rename", "merge", "season", "suggest_tags", "translate_recipe", "restructure_recipe"}


def apply(client, suggestion) -> None:
    d = suggestion.detail
    if suggestion.kind == "rename":
        rename(client, d.get("entity", "keyword"), d["id"], d["new_name"])
    elif suggestion.kind == "merge":
        merge(client, d.get("entity", "keyword"), d["keep_id"], d["keep_name"], d["remove_ids"])
    elif suggestion.kind == "season":
        add_tags(client, d["recipe_id"], [d["season"]])
    elif suggestion.kind == "suggest_tags":
        add_tags(client, d["recipe_id"], d["tags"])
    elif suggestion.kind == "translate_recipe":
        apply_translation(client, d["recipe_id"], d["translated"], d["shape"])
    elif suggestion.kind == "restructure_recipe":
        apply_restructure(client, d["recipe_id"], d["plan"])
    else:
        raise TandoorError(f"Unknown suggestion kind {suggestion.kind!r}")


# ---------- scans ----------

def _need_ai():
    if not llm_provider.is_configured():
        raise RuntimeError(llm_provider.missing_key_hint())


def _progress(job, label):
    job.progress_label = label
    tool_jobs.save_tool_job(job)


def _numbered(items):
    """Items with small numbers as ids for the AI, and the way back."""
    numbered = [{**item, "id": n} for n, item in enumerate(items, 1)]
    return numbered, {n: item["id"] for n, item in enumerate(items, 1)}


def _unnumber(action, back):
    action = dict(action)
    for field in ("id", "keep_id"):
        if field in action:
            action[field] = back[action[field]]
    if "remove_ids" in action:
        action["remove_ids"] = [back[i] for i in action["remove_ids"]]
    return action


def _describe(entity, action, by_id) -> str:
    return tools_new_recipes._describe(entity, action, by_id)


def tag_review(job, client):
    _need_ai()
    tags, back = _numbered(mealie_client.fetch_all_items(client, "keyword"))
    job.progress_total = len(tags)
    job.cost_estimate = format_cost_estimate(len(tags), "chunked_review")
    prompt = TAG_REVIEW_PROMPTS[job.tool].replace("{language}", settings.output_language)
    actions = []
    chunks = list(chunked(tags, 80))
    for i, chunk in enumerate(chunks, 1):
        if job.cancel_requested:
            break
        _progress(job, f"Reviewing chunk {i}/{len(chunks)}...")
        try:
            answer = tools_tags._complete_json(job, prompt, [{"id": t["id"], "name": t["name"]} for t in chunk], 8000)
            actions += [a for a in validate_actions(answer if isinstance(answer, list) else [], "tags_review")
                        if all(i in back for i in [a.get("id"), a.get("keep_id"), *a.get("remove_ids", [])] if i is not None)]
        except Exception as exc:  # noqa: BLE001
            log.warning("Tag review chunk %d failed: %s", i, exc)
        job.progress_current = min(i * 80, len(tags))
    by_id = {back[t["id"]]: {**t, "id": back[t["id"]]} for t in tags}
    for action in resolve_name_collisions(actions, tags):
        action = _unnumber(action, back)
        job.suggestions.append(ToolSuggestion(
            id=uuid.uuid4().hex[:10], kind=action["type"], summary=tools_tags._describe_action(action, by_id),
            detail={**action, "entity": "keyword"}))


def season(job, client):
    _need_ai()
    _progress(job, "Reading every recipe's full detail...")
    skip = ignored.keys("recipes_without_season")
    recipes = [view(r, for_tags=True) for r in fetch_recipes_full(client)]
    missing = [r for r in recipes if r["slug"] not in skip and not tools_tags.has_season_tag(r)]
    job.progress_total = len(missing)
    job.cost_estimate = format_cost_estimate(len(missing), "batched_season")
    job.suggestions = tools_tags.season_suggestions(job, missing)


def suggest_more(job, client):
    _need_ai()
    _progress(job, "Reading every recipe's full detail...")
    recipes = [view(r, for_tags=True) for r in fetch_recipes_full(client)]
    all_tags = mealie_client.fetch_all_items(client, "keyword")
    names = food_names(mealie_client.fetch_all_items(client, "food"))
    vocabulary = tools_tags.tag_vocabulary(all_tags, recipes, names)
    skip, checked, diets = ignored.keys("recipes_few_tags"), tools_tags.diet_checked(), tools_tags.diet_tags(vocabulary)
    todo = [r for r in recipes if r["slug"] not in skip
            and (few_tags(r, names) or (r["id"] not in checked and not tools_tags.has_diet_tag(r, diets)))]
    job.progress_total = len(todo)
    job.cost_estimate = format_cost_estimate(len(todo), "batched_suggest_tags")
    job.suggestions = tools_tags.suggest_tags_suggestions(job, todo, vocabulary, names)


def translate(job, client):
    _need_ai()
    _progress(job, "Looking for recipes to translate...")
    expected = get_language_code(settings.output_language)
    skip = ignored.keys("recipes_not_translated")
    todo = [v for v in (view(r) for r in fetch_recipes_full(client))
            if v["slug"] not in skip and not tools_recipes.already_in_target_language(v, expected)]
    job.progress_total = len(todo)
    job.cost_estimate = format_cost_estimate(len(todo), "per_recipe_translate")
    skipped = []
    for i, recipe in enumerate(todo, 1):
        if job.cancel_requested:
            break
        job.progress_current = i
        _progress(job, f"Translating recipe {i}/{len(todo)}...")
        suggestion, problem = translation_suggestion(job, recipe)
        if suggestion:
            job.suggestions.append(suggestion)
        else:
            skipped.append(problem)
    job.meta["skipped"] = skipped


def translation_suggestion(job, recipe):
    """(suggestion, None) or (None, why it was skipped)."""
    try:
        translated, usage = tools_recipes.translate_recipe_text(recipe, settings.output_language)
        job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
        job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
    except Exception as exc:  # noqa: BLE001
        log.warning("Translation failed for recipe %s: %s", recipe["slug"], exc)
        return None, {"name": recipe["name"], "reason": "failed", "error": str(exc)[:300]}
    preview = tools_recipes.describe_changes(recipe, translated)
    if not preview:
        ignored.add("recipes_not_translated", [{"key": recipe["slug"], "name": recipe["name"]}])
        return None, {"name": recipe["name"], "reason": "unchanged"}
    return ToolSuggestion(
        id=uuid.uuid4().hex[:10], kind="translate_recipe",
        summary=f"translate {recipe['name']!r} -> {translated['title']!r}",
        detail={"recipe_id": recipe["slug"], "translated": translated, "shape": tools_recipes._shape(recipe)},
        preview=preview), None


def restructure(job, client):
    _need_ai()
    _progress(job, "Checking the recipes' structure (no AI)...")
    skip = ignored.keys("recipes_need_restructure")
    todo = [v for v in (view(r) for r in fetch_recipes_full(client))
            if v["slug"] not in skip and recipe_restructure.needs_restructure(v)]
    job.progress_total = len(todo)
    job.cost_estimate = format_cost_estimate(len(todo), "per_recipe_translate")
    for i, recipe in enumerate(todo, 1):
        if job.cancel_requested:
            break
        job.progress_current = i
        _progress(job, f"Revising recipe {i}/{len(todo)}: {recipe['name']!r}...")
        suggestion = recipe_restructure.plan_suggestion(job, recipe)
        if suggestion:
            job.suggestions.append(suggestion)


# ---------- new recipes ----------

def list_recipe_ids(client) -> list[str]:
    return [r["slug"] for r in mealie_client._paged(client, "/recipes") if r.get("slug")]


def _only_used_by(client, param, item_id, slugs) -> bool:
    resp = mealie_client._check(client.get("/recipes", params={param: item_id, "perPage": 200}), "finding recipes")
    return all(r.get("slug") in slugs for r in resp.json().get("items", []))


def new_recipes(job, client):
    """Like tools_new_recipes.run_scan for Tandoor: 1. translate (applied
    right away), 1b. revise the structure, 2. match the ingredients, units
    and tags the new recipes introduced, 4. season and more tags. (Step 3,
    nutrition / categories / conversions, has no counterpart in Mealie.)"""
    _need_ai()
    _progress(job, "Looking for new recipes...")
    slugs = tools_new_recipes._new_recipe_ids(client, job.id)
    job.meta["recipe_ids"] = slugs
    job.progress_total = len(slugs)
    raw = []
    for slug in slugs:
        resp = client.get(f"/recipes/{slug}")
        if resp.status_code == 200:
            raw.append(resp.json())
    job.cost_estimate = (f"{len(raw)} new recipe(s): up to one AI call per recipe needing translation and one per "
                         f"recipe needing a structural revision, plus a few batched calls for ingredients, units and tags.")
    tool_jobs.save_tool_job(job)
    suggestions = []

    expected = get_language_code(settings.output_language)
    for i, recipe in enumerate(list(raw)):
        v = view(recipe)
        if job.cancel_requested:
            break
        if tools_recipes.already_in_target_language(v, expected):
            continue
        _progress(job, f"Translating {v['name']!r}...")
        suggestion, problem = translation_suggestion(job, v)
        if suggestion is None:
            if problem.get("reason") == "failed":
                suggestions.append(ToolSuggestion(id=uuid.uuid4().hex[:10], kind="translate_recipe", status="error",
                                                  summary=f"recipe: translate {v['name']!r}",
                                                  error=f"Translation failed: {problem.get('error')}"))
            continue
        suggestion.summary = f"recipe: translated {v['name']!r} -> {suggestion.detail['translated']['title']!r}"
        try:  # applied right away, like with Tandoor
            apply(client, suggestion)
            suggestion.status, suggestion.applied_at = "applied", time.time()
            raw[i] = _fetch(client, recipe["slug"])
        except Exception as exc:  # noqa: BLE001
            suggestion.status, suggestion.error = "error", f"Translation failed: {exc}"
        suggestions.append(suggestion)

    for recipe in raw:
        v = view(recipe)
        if job.cancel_requested:
            break
        if recipe_restructure.needs_restructure(v):
            _progress(job, f"Revising {v['name']!r}...")
            suggestion = recipe_restructure.plan_suggestion(job, v)
            if suggestion:
                suggestions.append(suggestion)

    final_tag_names = {}
    if not job.cancel_requested and raw:
        _progress(job, "Comparing units, ingredients and tags with existing ones...")
        used = {"food": {}, "unit": {}, "keyword": {}}
        for recipe in raw:
            for tag in recipe.get("tags") or []:
                used["keyword"][tag["id"]] = {"id": tag["id"], "name": tag["name"]}
            for row in recipe.get("recipeIngredient") or []:
                for entity in ("food", "unit"):
                    if (row.get(entity) or {}).get("id"):
                        used[entity][row[entity]["id"]] = {"id": row[entity]["id"], "name": row[entity]["name"]}
        slug_set = set(slugs)
        candidates = {
            "unit": list(used["unit"].values()),
            "food": [f for f in used["food"].values() if _only_used_by(client, "foods", f["id"], slug_set)],
            "keyword": [k for k in used["keyword"].values() if _only_used_by(client, "tags", k["id"], slug_set)],
        }
        for entity in ("unit", "food", "keyword"):
            if job.cancel_requested or not candidates[entity]:
                continue
            all_items, back = _numbered(mealie_client.fetch_all_items(client, entity))
            number = {uid: n for n, uid in back.items()}
            by_id = {back[i["id"]]: {**i, "id": back[i["id"]]} for i in all_items}
            try:
                actions = tools_new_recipes._match_actions(
                    job, entity, [{**c, "id": number[c["id"]]} for c in candidates[entity] if c["id"] in number], all_items)
            except Exception as exc:  # noqa: BLE001
                log.warning("Matching %s failed: %s", entity, exc)
                continue
            for action in actions:
                action = _unnumber(action, back)
                if entity == "keyword":
                    if action["type"] == "rename":
                        final_tag_names[action["id"]] = action["new_name"]
                    else:
                        for item_id in [action["keep_id"], *action["remove_ids"]]:
                            final_tag_names[item_id] = action["keep_name"]
                suggestions.append(ToolSuggestion(id=uuid.uuid4().hex[:10], kind=action["type"],
                                                  summary=_describe(entity, action, by_id), detail={**action, "entity": entity}))

        views = []
        for recipe in raw:
            v = view(recipe, for_tags=True)
            v["keywords"] = [{**kw, "name": final_tag_names.get(kw["id"], kw["name"])} for kw in v["keywords"]]
            views.append(v)
        if not job.cancel_requested:
            suggestions += tools_tags.season_suggestions(job, [v for v in views if not tools_tags.has_season_tag(v)])
        if not job.cancel_requested:
            all_tags = mealie_client.fetch_all_items(client, "keyword")
            names = food_names(mealie_client.fetch_all_items(client, "food"))
            suggestions += tools_tags.suggest_tags_suggestions(job, views, tools_tags.tag_vocabulary(all_tags, views, names), names)
    job.suggestions = suggestions


SCANS = {**{tool: tag_review for tool in TAG_REVIEW_PROMPTS}, "tags_season": season, "tags_suggest_more": suggest_more,
         "recipes_translate": translate, "recipes_restructure": restructure, "new_recipes": new_recipes}
