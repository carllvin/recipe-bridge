"""Ingredients, units and tags that no recipe uses (health tiles
"foods_unused" / "units_unused" / "keywords_unused") - left over from
imports, merges and renames. No AI: each suggestion deletes one entry, and
like every change it can be undone (it's recreated).

Kept although no recipe uses them:
- entries with sub-entries (a tag group like "Diet" or a food category),
- units used by a unit conversion or as a food's nutrition unit."""
from __future__ import annotations

import logging
import uuid

from . import ignored, recipe_scope, tandoor_client, tool_jobs
from .schemas import ToolSuggestion
from .tandoor_helpers import fetch_all_recipes_full
from .tools_conversions import _fetch_all

log = logging.getLogger("tandoor-helper")

# endpoint -> (health metric, tool)
KINDS = {
    "food": ("foods_unused", "unused_foods"),
    "unit": ("units_unused", "unused_units"),
    "keyword": ("keywords_unused", "unused_keywords"),
}
TOOL_ENDPOINT = {tool: endpoint for endpoint, (_, tool) in KINDS.items()}


def used_ids(recipes) -> dict[str, set[int]]:
    used = {"food": set(), "unit": set(), "keyword": set()}
    for recipe in recipes:
        used["keyword"].update(kw.get("id") for kw in recipe.get("keywords", []))
        for step in recipe.get("steps", []):
            for ing in step.get("ingredients", []):
                for endpoint in ("food", "unit"):
                    ref = ing.get(endpoint)
                    if ref and ref.get("id") is not None:
                        used[endpoint].add(ref["id"])
    return used


def protected_unit_ids(foods, conversions) -> set[int]:
    ids = set()
    for conv in conversions:
        for field in ("base_unit", "converted_unit"):
            if (conv.get(field) or {}).get("id") is not None:
                ids.add(conv[field]["id"])
    for food in foods:
        if (food.get("properties_food_unit") or {}).get("id") is not None:
            ids.add(food["properties_food_unit"]["id"])
    return ids


def find_unused(endpoint, items, used: set[int], protected=frozenset()) -> list[dict]:
    return sorted((i for i in items if i["id"] not in used and i["id"] not in protected and not i.get("numchild")),
                  key=lambda i: (i.get("name") or "").casefold())


def run_scan(job_id: str) -> None:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        return
    endpoint = TOOL_ENDPOINT[job.tool]
    metric = KINDS[endpoint][0]
    try:
        with tandoor_client.get_client() as client:
            job.progress_label = "Loading the entries..."
            tool_jobs.save_tool_job(job)
            items = _fetch_all(client, endpoint)
            protected = set()
            if endpoint == "unit":
                protected = protected_unit_ids(_fetch_all(client, "food"), _fetch_all(client, "unit-conversion"))
            # The overview already knows what no recipe used - only recipes
            # changed since then are read again. Without one: all recipes.
            known = recipe_scope.changed_since_overview(client, job)
            if known is None:
                job.progress_label = "Scanning every recipe's full detail..."
                tool_jobs.save_tool_job(job)
                used = used_ids(fetch_all_recipes_full(client))[endpoint]
            else:
                listed, changed = known
                candidates = {int(i["key"]) for i in listed.get(metric, []) if str(i["key"]).isdigit()}
                # everything the overview didn't list counts as used
                used = {i["id"] for i in items if i["id"] not in candidates} | used_ids(changed)[endpoint]
        skip = ignored.keys(metric)
        unused = [i for i in find_unused(endpoint, items, used, protected) if str(i["id"]) not in skip]
        label = {"food": "ingredient", "unit": "unit", "keyword": "tag"}[endpoint]
        job.suggestions = [
            ToolSuggestion(id=uuid.uuid4().hex[:10], kind="delete_unused",
                           summary=f"delete unused {label} {i.get('name', '')!r}",
                           detail={"endpoint": endpoint, "entity_id": i["id"], "name": i.get("name", "")})
            for i in unused
        ]
        job.cost_estimate = f"{len(unused)} unused {label}(s) - no AI involved."
        job.status = "ready"
        job.progress_label = None
        tool_jobs.save_tool_job(job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Unused-entries scan failed for job %s", job_id)
        job.status, job.error = "error", str(exc)
        tool_jobs.save_tool_job(job)


USAGE_FILTERS = {"food": "foods", "keyword": "keywords"}


def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise tandoor_client.TandoorError("Job not found.")
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise tandoor_client.TandoorError("Suggestion not found.")
    if suggestion.status != "pending":
        return suggestion
    d = suggestion.detail
    try:
        with tandoor_client.get_client() as client:
            param = USAGE_FILTERS.get(d["endpoint"])
            if param:  # a recipe may use it by now
                resp = client.get("/recipe/", params={param: d["entity_id"], "page_size": 1})
                data = resp.json() if resp.status_code == 200 else {}
                if resp.status_code != 200 or data.get("count") or data.get("results"):
                    raise tandoor_client.TandoorError("A recipe uses it by now - not deleted.")
            resp = client.delete(f"/{d['endpoint']}/{d['entity_id']}/")
            if resp.status_code not in (200, 202, 204, 404):
                raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status, suggestion.error = "error", str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion
