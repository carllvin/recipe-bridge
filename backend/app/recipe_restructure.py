"""Content revision for recipes that came in without the structure a
cookbook import gives them - typically Tandoor URL imports: all ingredients
hang on step 1, the method is one long block, servings/times are missing.

needs_restructure() decides locally (no AI) whether a recipe needs it.
restructure_plan() asks the AI once per such recipe to split the method
into sensible steps (keeping the wording), assign each ingredient to the
step that first uses it, and fill in missing servings/times. The result is
a suggestion with a before/after preview - applied only after review,
because unlike a translation it changes the recipe's structure.

Used by the new-recipes workflow and, for the whole collection, by the
"Recipes: revise structure" tool (run_scan / apply_suggestion below)."""
from __future__ import annotations

import json
import logging
import re

import uuid

from . import ignored, llm_provider, recipe_scope, tandoor_client, tool_jobs
from .config import settings
from .schemas import ToolSuggestion
from .tandoor_helpers import format_cost_estimate, minimal_ref

log = logging.getLogger("tandoor-helper")

LONG_SINGLE_STEP_CHARS = 250   # a single step longer than this ...
LONG_SINGLE_STEP_SENTENCES = 3  # ... or with at least this many sentences is probably several steps
MIN_KEPT_TEXT_RATIO = 0.7      # a plan that loses more text than this dropped content -> rejected

SYSTEM_PROMPT = """You improve the STRUCTURE of a recipe written in {language}
without changing its content. You will receive a JSON object:
{"title": string, "servings": int|null, "working_time": int|null,
"waiting_time": int|null, "steps": [{"title": string|null, "instruction": string}],
"ingredients": [{"key": string, "text": string}]}

1. Steps: split a step that does several distinct things into separate
   steps, in the original order (e.g. prepare the dough / make the filling /
   assemble / bake). Keep the original wording - only fix obvious typos or
   formatting. Never add, drop or invent content; keep every amount,
   temperature and time. Give a step a short "title" only where it clearly
   helps (e.g. "Teig", "Füllung"), otherwise null.
2. Assign EVERY ingredient key to exactly one step: the step where that
   ingredient is first used.
3. servings, working_time (active minutes), waiting_time (baking, resting,
   chilling minutes): keep values that are given; if missing, estimate from
   the text, or use null if it can't be told.

Respond with ONLY a JSON object (no explanation, no markdown fence):
{"steps": [{"title": string|null, "instruction": string, "ingredients": [<keys>]}],
 "servings": int|null, "working_time": int|null, "waiting_time": int|null}
"""


def _ingredients(recipe):
    return [ing for step in recipe.get("steps", []) for ing in step.get("ingredients", [])]


def _ingredient_text(ing) -> str:
    parts = []
    if ing.get("amount") and not ing.get("no_amount"):
        parts.append(f"{ing['amount']:g}" if isinstance(ing["amount"], (int, float)) else str(ing["amount"]))
    if ing.get("unit"):
        parts.append(ing["unit"].get("name", ""))
    parts.append((ing.get("food") or {}).get("name", ""))
    text = " ".join(p for p in parts if p)
    if ing.get("note"):
        text += f" ({ing['note']})"
    return text


def _text_length(recipe) -> int:
    return sum(len(s.get("instruction") or "") for s in recipe.get("steps", []))


def needs_restructure(recipe) -> list[str]:
    """Reasons this recipe would benefit from a revision - empty if none.
    Recipes using Jinja templates in their steps ({{ ingredients[0] }}) are
    skipped: those reference ingredients by position within a step, which a
    re-split would break."""
    steps = recipe.get("steps", [])
    if not steps or any("{{" in (s.get("instruction") or "") for s in steps):
        return []
    reasons = []
    ingredients = _ingredients(recipe)
    if len(steps) >= 2 and ingredients and all(not s.get("ingredients") for s in steps[1:]):
        reasons.append("all ingredients in step 1")
    if len(steps) == 1:
        text = steps[0].get("instruction") or ""
        sentences = len(re.findall(r"[.!?](\s|$)", text))
        if len(text) > LONG_SINGLE_STEP_CHARS or sentences >= LONG_SINGLE_STEP_SENTENCES:
            reasons.append("one long step")
    if not recipe.get("servings"):
        reasons.append("servings missing")
    if not recipe.get("working_time") and not recipe.get("waiting_time"):
        reasons.append("times missing")
    # Missing servings/times alone aren't worth a full revision call.
    return reasons if any(r in reasons for r in ("all ingredients in step 1", "one long step")) else []


def restructure_plan(recipe, language) -> tuple[dict, object]:
    """One AI call (main model - it writes the visible step text). Returns a
    validated plan and the token usage; raises ValueError for a plan that
    can't be trusted."""
    ingredients = _ingredients(recipe)
    keys = {f"i{n}": ing for n, ing in enumerate(ingredients)}
    payload = {
        "title": recipe.get("name", ""),
        "servings": recipe.get("servings") or None,
        "working_time": recipe.get("working_time") or None,
        "waiting_time": recipe.get("waiting_time") or None,
        "steps": [{"title": s.get("name") or None, "instruction": s.get("instruction") or ""}
                  for s in recipe.get("steps", [])],
        "ingredients": [{"key": k, "text": _ingredient_text(ing)} for k, ing in keys.items()],
    }
    text_out, usage = llm_provider.complete_text(
        SYSTEM_PROMPT.replace("{language}", language), json.dumps(payload, ensure_ascii=False), max_tokens=6000
    )
    text_out = text_out.strip()
    if text_out.startswith("```"):
        text_out = text_out.strip("`")
        if text_out.startswith("json"):
            text_out = text_out[4:]
    plan = json.loads(text_out)

    steps = [s for s in plan.get("steps") or [] if isinstance(s, dict) and (s.get("instruction") or "").strip()]
    if not steps:
        raise ValueError("revision returned no steps")
    new_length = sum(len(s["instruction"]) for s in steps)
    if new_length < MIN_KEPT_TEXT_RATIO * _text_length(recipe):
        raise ValueError(f"revision dropped too much text ({new_length} of {_text_length(recipe)} chars) - not used")

    # Every ingredient exactly once; unassigned ones go to step 1.
    assigned = set()
    for step in steps:
        own = []
        for key in step.get("ingredients") or []:
            if key in keys and key not in assigned:
                assigned.add(key)
                own.append(key)
        step["ingredients"] = own
        step["title"] = (step.get("title") or None) if isinstance(step.get("title"), (str, type(None))) else None
    # Ingredients the AI didn't (validly) assign: to the first step whose
    # text mentions them, otherwise to step 1.
    for key in (k for k in keys if k not in assigned):
        name = ((keys[key].get("food") or {}).get("name") or "").lower()
        target = next((st for st in steps if name and name in st["instruction"].lower()), steps[0])
        target["ingredients"].append(key)

    def _minutes(value):
        return int(value) if isinstance(value, (int, float)) and 0 < value < 60 * 48 else None

    return {
        "steps": [{"title": s["title"], "instruction": s["instruction"].strip(), "ingredients": s["ingredients"]}
                  for s in steps],
        "servings": int(plan["servings"]) if isinstance(plan.get("servings"), (int, float)) and 0 < plan["servings"] < 100 else None,
        "working_time": _minutes(plan.get("working_time")),
        "waiting_time": _minutes(plan.get("waiting_time")),
        # key -> ingredient row id, so applying works by id even if rows were
        # re-ordered in the meantime (e.g. by an ingredient merge)
        "keys": {k: ing.get("id") for k, ing in keys.items()},
    }, usage


def describe_plan(recipe, plan) -> tuple[str, str]:
    """(one-line summary, multi-line before/after preview)."""
    ingredients = _ingredients(recipe)
    keys = {f"i{n}": ing for n, ing in enumerate(ingredients)}
    old_steps = recipe.get("steps", [])
    changes = [f"{len(old_steps)} -> {len(plan['steps'])} step(s)", "ingredients assigned to steps"]
    lines = ["BEFORE:"]
    for n, step in enumerate(old_steps, 1):
        names = ", ".join((i.get("food") or {}).get("name", "?") for i in step.get("ingredients", [])) or "-"
        lines.append(f"  {n}. {(step.get('instruction') or '')[:70]}…  [{names}]")
    lines.append("AFTER:")
    for n, step in enumerate(plan["steps"], 1):
        names = ", ".join((keys[k].get("food") or {}).get("name", "?") for k in step["ingredients"]) or "-"
        title = f"{step['title']}: " if step.get("title") else ""
        lines.append(f"  {n}. {title}{step['instruction'][:70]}…  [{names}]")
    for field, label in (("servings", "servings"), ("working_time", "working time (min)"), ("waiting_time", "waiting time (min)")):
        if plan.get(field) and not recipe.get(field):
            lines.append(f"{label}: - -> {plan[field]}")
            changes.append(f"{label} {plan[field]}")
    return f"recipe: revise {recipe.get('name', '')!r} - " + ", ".join(changes), "\n".join(lines)


def _resolve_keys(recipe, plan) -> dict:
    by_id = {ing.get("id"): ing for ing in _ingredients(recipe)}
    return {key: by_id[row_id] for key, row_id in plan["keys"].items() if row_id in by_id}


def build_payload(recipe, plan) -> dict:
    """PATCH payload: the recipe's existing steps are reused (by position)
    for the new ones, extra steps are added, surplus old steps dropped;
    ingredients keep their ids and just move between steps. Servings/times
    are only filled where the recipe has none."""
    keys = _resolve_keys(recipe, plan)
    old_steps = recipe.get("steps", [])
    new_steps = []
    for n, planned in enumerate(plan["steps"]):
        step = dict(old_steps[n]) if n < len(old_steps) else {"time": 0}
        step["name"] = (planned.get("title") or "")[:128]
        step["instruction"] = planned["instruction"]
        step["order"] = n
        moved = []
        for order, key in enumerate(planned["ingredients"]):
            ing = dict(keys[key])
            for field in ("food", "unit"):
                if ing.get(field) is not None:
                    ing[field] = minimal_ref(ing[field])
            ing["order"] = order
            moved.append(ing)
        step["ingredients"] = moved
        new_steps.append(step)
    payload = {"steps": new_steps}
    for field in ("servings", "working_time", "waiting_time"):
        if plan.get(field) and not recipe.get(field):
            payload[field] = plan[field]
    return payload


def same_ingredients(recipe, plan) -> bool:
    """The plan still fits: same ingredient rows as when it was made."""
    current = sorted(i.get("id") or 0 for i in _ingredients(recipe))
    return None not in plan["keys"].values() and current == sorted(plan["keys"].values())


def apply_plan(job, suggestion) -> ToolSuggestion:
    """Applies a revision suggestion against a FRESH copy of the recipe
    (ingredient merges may have run since the scan)."""
    if suggestion.status != "pending":
        return suggestion
    try:
        with tandoor_client.get_client() as client:
            recipe_id = suggestion.detail["recipe_id"]
            resp = client.get(f"/recipe/{recipe_id}/")
            resp.raise_for_status()
            recipe = resp.json()
            plan = suggestion.detail["plan"]
            if not same_ingredients(recipe, plan):
                raise tandoor_client.TandoorError("The recipe's ingredients changed since the scan - rescan to revise it.")
            resp = client.patch(f"/recipe/{recipe_id}/", json=build_payload(recipe, plan))
            if resp.status_code not in (200, 201):
                raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status = "error"
        suggestion.error = str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion


def plan_suggestion(job, recipe) -> ToolSuggestion | None:
    """AI revision for one recipe as a suggestion (None if it failed)."""
    try:
        plan, usage = restructure_plan(recipe, settings.output_language)
        job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
        job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
    except Exception as exc:  # noqa: BLE001
        log.warning("Revision of recipe %s failed: %s", recipe.get("id"), exc)
        return None
    summary, preview = describe_plan(recipe, plan)
    return ToolSuggestion(id=uuid.uuid4().hex[:10], kind="restructure_recipe", summary=summary, preview=preview,
                          detail={"recipe_id": recipe["id"], "plan": plan})


def run_scan(job_id: str) -> None:
    """The whole collection: checks every recipe locally (no AI), then one
    AI call per recipe that needs a revision."""
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
            job.progress_label = "Checking the recipes' structure (no AI)..."
            tool_jobs.save_tool_job(job)
            candidates = recipe_scope.recipes_for(client, "recipes_need_restructure", lambda r: bool(needs_restructure(r)),
                                                  ignored.keys("recipes_need_restructure"), job)
        job.progress_total = len(candidates)
        job.cost_estimate = format_cost_estimate(len(candidates), "per_recipe_translate")
        tool_jobs.save_tool_job(job)

        suggestions = []
        for i, recipe in enumerate(candidates, 1):
            if job.cancel_requested:
                break
            job.progress_current = i
            job.progress_label = f"Revising recipe {i}/{len(candidates)}: {recipe.get('name', '')!r}..."
            tool_jobs.save_tool_job(job)
            suggestion = plan_suggestion(job, recipe)
            if suggestion:
                suggestions.append(suggestion)

        job.suggestions = suggestions
        job.status = "cancelled" if job.cancel_requested else "ready"
        job.progress_label = None
        tool_jobs.save_tool_job(job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Recipe revision scan failed for job %s", job_id)
        job.status = "error"
        job.error = str(exc)
        tool_jobs.save_tool_job(job)


def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise tandoor_client.TandoorError("Job not found.")
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise tandoor_client.TandoorError("Suggestion not found.")
    return apply_plan(job, suggestion)
