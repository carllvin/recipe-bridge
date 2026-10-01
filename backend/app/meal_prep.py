"""One work plan for several recipes - for a meal-prep session (the week's
planned days cooked ahead in one go) or a guest menu (everything ready at
a given time).

The AI reads the recipes' steps (scaled to the persons) and merges them:
shared preparation done once ("chop all onions"), oven and hob used in
parallel, the longest-running things first, what can be done ahead marked
as such - with a guest menu as clock times counting back from "ready at".
The plan is kept in the source run's meta["prep"] so it's still there
after a reload."""
from __future__ import annotations

import json
import logging
import re

from . import household, json_answer, llm_provider, target, tandoor_client, tool_jobs
from .config import settings

log = logging.getLogger("recipe-bridge")

MAX_RECIPES = 8

PROMPT = """You turn several recipes into ONE work plan for a home cook,
written in {language}. You will receive a JSON object:
{"mode": "prep" | "menu", "ready_at": "HH:MM" | null, "persons": int|null,
"recipes": [{"name": string, "scale": number, "servings": number|null,
"ingredients": [string], "steps": [{"text": string, "minutes": int|null}]}]}

"prep": a meal-prep session - cook or prepare the recipes ahead for the
coming days; say how to keep each part and what to finish on the day.
"menu": everything is served together at "ready_at" (if given) - give
clock times, counting back from "ready_at".

Rules:
- do shared preparation once (e.g. "dice the onions for A and B: 3 onions")
  with the combined amounts ("scale" = the factor for the persons)
- start what takes longest first; use oven and hob in parallel; put
  waiting times (baking, simmering, resting, chilling) to use
- keep every amount, temperature and time of the recipes; invent nothing
- each task names the recipe(s) it is for

Respond with ONLY a JSON object (no explanation, no markdown fence):
{"total_minutes": int, "phases": [{"title": string, "time": "HH:MM" | null,
 "tasks": [{"text": string, "recipes": [string], "minutes": int|null}]}],
 "keeping": [string]}
("keeping": how to store prepared parts - for "prep"; else []).
"""


def _text_steps(recipe) -> list[dict]:
    return [{"text": (s.get("instruction") or "").strip(), "minutes": s.get("time") or None}
            for s in recipe.get("steps") or [] if (s.get("instruction") or "").strip()]


def _ingredient_lines(recipe, scale) -> list[str]:
    lines = []
    for step in recipe.get("steps") or []:
        for ing in step.get("ingredients") or []:
            food = (ing.get("food") or {}).get("name")
            if not food or ing.get("is_header"):
                continue
            amount = None if ing.get("no_amount") or not ing.get("amount") else round(float(ing["amount"]) * scale, 2)
            unit = (ing.get("unit") or {}).get("name") or ""
            lines.append(" ".join(p for p in (f"{amount:g}" if amount else "", unit, food) if p))
    return lines


def _load(client, wanted) -> dict | None:
    if target.is_mealie():
        from .mealie_tools import view
        resp = client.get(f"/recipes/{wanted.get('slug') or wanted['id']}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        recipe = view(data, for_tags=True)
        recipe["servings"] = data.get("recipeServings") or data.get("recipeYieldQuantity") or 0
        return recipe
    resp = client.get(f"/recipe/{wanted['id']}/")
    return resp.json() if resp.status_code == 200 else None


def plan(job, recipes: list[dict], persons: int | None, mode: str, ready_at: str | None = None) -> dict:
    """recipes: [{"id", "slug"?, "name"}]. One AI call; returns the plan."""
    if not llm_provider.is_configured():
        raise tandoor_client.TandoorError(llm_provider.missing_key_hint())
    if not recipes:
        raise tandoor_client.TandoorError("No recipes to plan.")
    persons = persons or household.get()["persons"] or None
    loaded = []
    with target.client().get_client() as client:
        for wanted in recipes[:MAX_RECIPES]:
            recipe = _load(client, wanted)
            if recipe is None:
                continue
            servings = recipe.get("servings") or 0
            scale = round(persons / servings, 2) if persons and servings else 1
            loaded.append({"name": wanted.get("name") or recipe.get("name", ""), "scale": scale,
                           "servings": servings or None, "ingredients": _ingredient_lines(recipe, scale),
                           "steps": _text_steps(recipe)})
    if not loaded:
        raise tandoor_client.TandoorError("The recipes couldn't be read.")
    if ready_at and not re.fullmatch(r"\d{1,2}:\d{2}", ready_at):
        ready_at = None
    payload = {"mode": mode, "ready_at": ready_at, "persons": persons, "recipes": loaded}

    def ask(system_prompt):
        out, usage = llm_provider.complete_tool_text(system_prompt, json.dumps(payload, ensure_ascii=False),
                                                     max_tokens=4000)
        job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
        job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
        return out, usage
    answer = json_answer.complete(ask, PROMPT.replace("{language}", settings.output_language))[0]
    answer = answer if isinstance(answer, dict) else {}
    names = {r["name"] for r in loaded}
    phases = []
    for phase in answer.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        tasks = [{"text": str(t.get("text") or "").strip(),
                  "recipes": [n for n in t.get("recipes") or [] if n in names],
                  "minutes": t.get("minutes") if isinstance(t.get("minutes"), int) else None}
                 for t in phase.get("tasks") or [] if isinstance(t, dict) and str(t.get("text") or "").strip()]
        if tasks:
            time = phase.get("time") if isinstance(phase.get("time"), str) and re.fullmatch(r"\d{1,2}:\d{2}", phase["time"]) else None
            phases.append({"title": str(phase.get("title") or "").strip(), "time": time, "tasks": tasks})
    if not phases:
        raise tandoor_client.TandoorError("The AI returned no usable plan - try again.")
    result = {"mode": mode, "ready_at": ready_at, "persons": persons, "recipes": [r["name"] for r in loaded],
              "total_minutes": answer.get("total_minutes") if isinstance(answer.get("total_minutes"), int) else None,
              "phases": phases, "keeping": [str(k) for k in answer.get("keeping") or [] if str(k).strip()]}
    job.meta["prep"] = result
    tool_jobs.save_tool_job(job)
    return result


def for_job(job_id: str, ready_at: str | None = None, suggestion_ids: list | None = None) -> dict:
    """The plan for a weekly plan (its open days, or the given ones) or a
    guest menu."""
    job = tool_jobs.get_tool_job(job_id)
    if job is None or job.tool not in ("meal_plan", "guest_menu"):
        raise tandoor_client.TandoorError("Plan not found.")
    if job.tool == "guest_menu":
        recipes = [m["recipe"] for m in job.meta.get("menu", [])]
        return plan(job, recipes, job.meta["params"]["persons"], "menu", ready_at)
    chosen = [s for s in job.suggestions if s.status != "skipped" and (not suggestion_ids or s.id in suggestion_ids)]
    chosen.sort(key=lambda s: s.detail["date"])
    return plan(job, [s.detail["recipe"] for s in chosen], None, "prep")
