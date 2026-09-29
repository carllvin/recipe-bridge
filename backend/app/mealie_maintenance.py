"""The maintenance tiles that also work with Mealie (RECIPE_MANAGER=mealie):
possible duplicate ingredients / units, unused ingredients / units / tags,
recipes without servings or without a photo - and, via mealie_tools, the
AI tools for tags, translating and revising recipes and new recipes.

- compute(): the health overview's entries (same shape as health.py's) -
  reading every recipe in full, like for Tandoor;
- the tools keep the names of their Tandoor counterparts (so tiles, runs and
  the review work unchanged); main.py routes them here when the run was
  started for Mealie (job.meta["target"] == "mealie").

Changes in Mealie can't be undone from the helper (the undo journal replays
Tandoor's API) - each suggestion is still reviewed before it's applied."""
from __future__ import annotations

import logging
import os
import tempfile
import uuid

from . import duplicates, ignored, image_gen, llm_provider, mealie_client, mealie_tools, tool_jobs, tools_recipe_details, tools_tags
from .config import settings
from .schemas import ToolSuggestion
from .tandoor_client import TandoorError
from .tools_unused import find_unused

log = logging.getLogger("recipe-bridge")

METRICS = ["foods_duplicates", "units_duplicates", "foods_unused", "units_unused", "keywords_unused",
           "recipes_without_servings", "recipes_without_image", *mealie_tools.METRICS]
TOOLS = ["ingredients_review", "units_review", "unused_foods", "unused_units", "unused_keywords",
         "recipes_servings", "recipes_images", *mealie_tools.TOOLS]
ENTITY_PATHS = {"food": "/foods", "unit": "/units", "keyword": "/organizers/tags"}
UNUSED_TOOLS = {"unused_foods": ("food", "foods_unused"), "unused_units": ("unit", "units_unused"),
                "unused_keywords": ("keyword", "keywords_unused")}
DUPLICATE_TOOLS = {"ingredients_review": ("food", "foods_duplicates"), "units_review": ("unit", "units_duplicates")}


# ---------- reading ----------

def fetch_recipes_full(client) -> list[dict]:
    full = []
    for item in mealie_client._paged(client, "/recipes"):
        resp = client.get(f"/recipes/{item['slug']}")
        if resp.status_code == 200:
            full.append(resp.json())
    return full


def used_ids(recipes) -> dict[str, dict]:
    """entity -> {id: number of recipes using it}"""
    used = {"food": {}, "unit": {}, "keyword": {}}
    for recipe in recipes:
        seen = {"food": set(), "unit": set(), "keyword": {t.get("id") for t in recipe.get("tags") or []}}
        for ing in recipe.get("recipeIngredient") or []:
            for entity in ("food", "unit"):
                if (ing.get(entity) or {}).get("id"):
                    seen[entity].add(ing[entity]["id"])
        for entity, ids in seen.items():
            for i in ids:
                used[entity][i] = used[entity].get(i, 0) + 1
    return used


def lacks_servings(recipe) -> bool:
    return not (recipe.get("recipeServings") or recipe.get("recipeYieldQuantity"))


def lacks_image(recipe) -> bool:
    return not recipe.get("image")


def ingredient_lines(recipe) -> list[str]:
    lines = []
    for ing in recipe.get("recipeIngredient") or []:
        food = (ing.get("food") or {}).get("name")
        if not food:
            continue
        unit = (ing.get("unit") or {}).get("name") or ""
        qty = ing.get("quantity")
        lines.append(" ".join(p for p in (f"{qty:g}" if isinstance(qty, (int, float)) and qty else "", unit, food) if p))
    return lines[:tools_recipe_details.MAX_INGREDIENT_LINES]


def compute(client) -> tuple[dict, dict]:
    """(metrics, items per metric) for the health overview."""
    recipes = fetch_recipes_full(client)
    try:  # "What can I cook today?" reads the same recipes - like health.py does for Tandoor
        from . import cook_today, mealie_plan
        cook_today.save_index([mealie_plan.as_tandoor(r) for r in recipes])
    except Exception:  # noqa: BLE001
        log.exception("Saving the recipe index failed")
    lists = {entity: mealie_client.fetch_all_items(client, entity) for entity in ENTITY_PATHS}
    used = used_ids(recipes)

    def recipe_items(matches):
        return [{"key": r["slug"], "name": r.get("name", ""), "recipe_id": r["slug"]}
                for r in sorted(recipes, key=lambda r: (r.get("name") or "").casefold()) if matches(r)]

    items = {
        "foods_duplicates": [{"key": p["key"], "name": p["name"]} for p in duplicates.food_duplicates(lists["food"])],
        "units_duplicates": [{"key": p["key"], "name": p["name"]} for p in duplicates.unit_duplicates(lists["unit"])],
        **{metric: [{"key": str(i["id"]), "name": i["name"]}
                    for i in find_unused(entity, lists[entity], set(used[entity]))]
           for entity, metric in (("food", "foods_unused"), ("unit", "units_unused"), ("keyword", "keywords_unused"))},
        "recipes_without_servings": recipe_items(lambda r: lacks_servings(r) and ingredient_lines(r)),
        "recipes_without_image": recipe_items(lacks_image),
        **mealie_tools.metric_items(recipes, lists["food"]),
    }
    return {"recipes_total": len(recipes), "foods_used": len(used["food"])}, items


# ---------- scans ----------

def _run(job_id, body):
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        return
    try:
        with mealie_client.get_client() as client:
            body(job, client)
        job.status = "cancelled" if job.cancel_requested else "ready"
        job.progress_label = None
        tool_jobs.save_tool_job(job)
        if job.tool == "new_recipes":
            from . import tools_new_recipes
            tools_new_recipes.after_action(job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Mealie maintenance scan failed for job %s", job_id)
        job.status, job.error = "error", str(exc)
        tool_jobs.save_tool_job(job)


def _progress(job, label):
    job.progress_label = label
    tool_jobs.save_tool_job(job)


def _duplicates(job, client):
    entity, metric = DUPLICATE_TOOLS[job.tool]
    _progress(job, "Reading every recipe's full detail...")
    counts = used_ids(fetch_recipes_full(client))[entity]
    items = {i["id"]: i for i in mealie_client.fetch_all_items(client, entity)}
    finder = duplicates.food_duplicates if entity == "food" else duplicates.unit_duplicates
    skip = ignored.keys(metric)
    for pair in finder(list(items.values())):
        if pair["key"] in skip:
            continue
        # keep the one more recipes use (on a tie: the shorter name)
        keep, remove = sorted((items[i] for i in pair["ids"]),
                              key=lambda i: (-counts.get(i["id"], 0), len(i["name"]), i["name"]))
        job.suggestions.append(ToolSuggestion(
            id=uuid.uuid4().hex[:10], kind="merge",
            summary=f"merge {remove['name']!r} into {keep['name']!r}",
            detail={"entity": entity, "keep_id": keep["id"], "keep_name": keep["name"],
                    "remove_id": remove["id"], "remove_name": remove["name"]},
        ))
    job.cost_estimate = f"{len(job.suggestions)} possible duplicate(s) - no AI involved."


def _unused(job, client):
    entity, metric = UNUSED_TOOLS[job.tool]
    _progress(job, "Reading every recipe's full detail...")
    used = set(used_ids(fetch_recipes_full(client))[entity])
    skip = ignored.keys(metric)
    label = {"food": "ingredient", "unit": "unit", "keyword": "tag"}[entity]
    for item in find_unused(entity, mealie_client.fetch_all_items(client, entity), used):
        if str(item["id"]) in skip:
            continue
        job.suggestions.append(ToolSuggestion(
            id=uuid.uuid4().hex[:10], kind="delete_unused", summary=f"delete unused {label} {item['name']!r}",
            detail={"endpoint": entity, "entity_id": item["id"], "name": item["name"]},
        ))
    job.cost_estimate = f"{len(job.suggestions)} unused {label}(s) - no AI involved."


def _servings(job, client):
    if not llm_provider.is_configured():
        raise RuntimeError(llm_provider.missing_key_hint())
    _progress(job, "Reading every recipe's full detail...")
    skip = ignored.keys("recipes_without_servings")
    recipes = [r for r in fetch_recipes_full(client)
               if r["slug"] not in skip and lacks_servings(r) and ingredient_lines(r)]
    by_slug = {r["slug"]: r for r in recipes}
    job.progress_total = len(recipes)
    for batch_no, start in enumerate(range(0, len(recipes), tools_recipe_details.SERVINGS_BATCH_SIZE), 1):
        if job.cancel_requested:
            break
        batch = recipes[start:start + tools_recipe_details.SERVINGS_BATCH_SIZE]
        _progress(job, f"Estimating servings, batch {batch_no}...")
        answers = tools_tags._complete_json(
            job, tools_recipe_details.SERVINGS_SYSTEM_PROMPT,
            [{"id": i, "title": r.get("name", ""), "ingredients": ingredient_lines(r)} for i, r in enumerate(batch)],
            max_tokens=20 * len(batch) + 100)
        for answer in answers if isinstance(answers, list) else []:
            if not isinstance(answer, dict) or not isinstance(answer.get("id"), int) or not 0 <= answer["id"] < len(batch):
                continue
            servings = answer.get("servings")
            if not isinstance(servings, (int, float)) or not 2 <= servings <= 100:
                continue
            recipe = by_slug[batch[answer["id"]]["slug"]]
            job.suggestions.append(ToolSuggestion(
                id=uuid.uuid4().hex[:10], kind="set_servings",
                summary=f"{recipe.get('name', '')!r}: {int(round(servings))} servings",
                detail={"recipe_id": recipe["slug"], "recipe_name": recipe.get("name", ""), "servings": int(round(servings))},
            ))
        job.progress_current = min(start + len(batch), len(recipes))


def _images(job, client):
    if not image_gen.is_configured():
        raise RuntimeError(image_gen.missing_key_hint())
    skip = ignored.keys("recipes_without_image")
    _progress(job, "Looking for recipes without a photo...")
    for recipe in mealie_client._paged(client, "/recipes"):
        if recipe["slug"] in skip or not lacks_image(recipe):
            continue
        job.suggestions.append(ToolSuggestion(
            id=uuid.uuid4().hex[:10], kind="generate_image", summary=f"generate a photo for {recipe.get('name', '')!r}",
            detail={"recipe_id": recipe["slug"], "recipe_name": recipe.get("name", ""),
                    "description": (recipe.get("description") or "")[:300],
                    "tags": [t["name"] for t in recipe.get("tags") or []][:8]},
        ))
    job.cost_estimate = (f"{len(job.suggestions)} recipe(s) without a photo. Nothing is generated yet - each "
                         f"photo is generated (and paid) only when you apply its suggestion.")


SCANS = {"ingredients_review": _duplicates, "units_review": _duplicates, "unused_foods": _unused,
         "unused_units": _unused, "unused_keywords": _unused, "recipes_servings": _servings, "recipes_images": _images,
         **mealie_tools.SCANS}


def run_scan(job_id: str) -> None:
    job = tool_jobs.get_tool_job(job_id)
    if job is not None:
        _run(job_id, SCANS[job.tool])


# ---------- applying ----------

def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise TandoorError("Job not found.")
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise TandoorError("Suggestion not found.")
    if suggestion.status != "pending":
        return suggestion
    d = suggestion.detail
    path = None
    try:
        with mealie_client.get_client() as client:
            if suggestion.kind in mealie_tools.KINDS and not (suggestion.kind == "merge" and "remove_id" in d):
                mealie_tools.apply(client, suggestion)
            elif suggestion.kind == "merge":
                body = ({"fromFood": d["remove_id"], "toFood": d["keep_id"]} if d["entity"] == "food"
                        else {"fromUnit": d["remove_id"], "toUnit": d["keep_id"]})
                mealie_client._check(client.put(f"{ENTITY_PATHS[d['entity']]}/merge", json=body), "the merge")
            elif suggestion.kind == "delete_unused":
                resp = client.delete(f"{ENTITY_PATHS[d['endpoint']]}/{d['entity_id']}")
                if resp.status_code != 404:
                    mealie_client._check(resp, "deleting")
            elif suggestion.kind == "set_servings":
                mealie_client._check(client.patch(f"/recipes/{d['recipe_id']}", json={"recipeServings": d["servings"]}),
                                     "the servings")
            elif suggestion.kind == "generate_image":
                current = mealie_client._check(client.get(f"/recipes/{d['recipe_id']}"), "loading the recipe").json()
                if current.get("image"):
                    suggestion.status = "applied"  # got a photo in the meantime
                    return suggestion
                data = image_gen.generate_image(
                    image_gen.build_recipe_image_prompt(d["recipe_name"], d.get("description"), d.get("tags") or []))
                fd, path = tempfile.mkstemp(suffix=".png", dir=settings.data_dir if os.path.isdir(settings.data_dir) else None)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                mealie_client.upload_image(client, d["recipe_id"], path)
            else:
                raise TandoorError(f"Unknown suggestion kind {suggestion.kind!r}")
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status, suggestion.error = "error", str(exc)
    finally:
        if path and os.path.exists(path):
            os.remove(path)
        tool_jobs.save_tool_job(job)
    if job.tool == "new_recipes":
        from . import tools_new_recipes
        tools_new_recipes.after_action(job)
    return suggestion
