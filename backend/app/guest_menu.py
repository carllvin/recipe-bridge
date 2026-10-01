"""Plan -> guest menu: "Saturday, 8 persons, 3 courses, one guest without
gluten" - the AI puts together a menu from your own recipes (one per
course, matching each other), the household's and the guests' "never"
ingredients are left out before it sees the list. The menu can be changed
course by course, goes into the meal plan with the number of guests as
servings, and has its own shopping list (shopping_text) and - via
meal_prep - a work plan for the day.

A menu is a tool run ("guest_menu") without suggestions; everything lives
in its meta: {"params", "menu": [{"course", "recipe", "reason"}], "note"}."""
from __future__ import annotations

import datetime as dt
import json
import logging

from . import (cook_today, household, json_answer, llm_provider, mealie_plan, seasonal, tandoor_client, target,
               tool_jobs, tools_meal_plan)
from .config import settings

log = logging.getLogger("recipe-bridge")

TOOL = "guest_menu"
COURSES = ["starter", "soup", "main", "side", "dessert"]
MAX_CANDIDATES = 300

PROMPT = """You plan a menu for guests from a home cook's own recipe
collection. Language for "reason" and "note": {language}. You will receive
a JSON object: {"occasion": string, "date": "YYYY-MM-DD (weekday)", "persons": int,
"courses": [string], "guests_never": [string], "wishes": string, "in_season": [string],
"keep": [{"course", "recipe_id"}], "recipes": [RECIPE, ...]}
""" + tools_meal_plan.RECIPE_LINE + """
Pick exactly one recipe for each of "courses" (in that order) - except the
ones in "keep", which stay as they are:
- the recipe must fit its course: "starter" a small, light first course,
  "soup" a soup, "main" a main dish, "side" a side dish that goes with the
  main, "dessert" something sweet
- NEVER pick a recipe whose title, tags or ingredients contain anything in
  "guests_never" (allergies of the guests)
- the courses must go together as one menu: no main ingredient twice,
  varied (not three creamy or heavy courses), matching cuisines
- for guests: well-rated recipes, ideally ones that can be partly prepared
  ahead, a manageable total effort for the number of persons
- the season ("in_season"), the occasion and the wishes
- every recipe at most once

Respond with ONLY a JSON object (no explanation, no markdown fence):
{"courses": [{"course": string, "recipe_id": <id>, "reason": <a few words>}],
 "note": <one sentence about the menu>}
"""


def _date(value) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return dt.date.today()


def params_from(body: dict) -> dict:
    courses = [c for c in COURSES if c in (body.get("courses") or [])] or ["starter", "main", "dessert"]
    return {
        "occasion": str(body.get("occasion") or "").strip()[:200],
        "date": _date(body.get("date")).isoformat(),
        "persons": min(50, max(1, int(body.get("persons") or 4))),
        "courses": courses,
        "guests_never": str(body.get("guests_never") or "").strip()[:500],
        "wishes": str(body.get("wishes") or "").strip()[:500],
    }


def _recipes(client, params) -> list[dict]:
    everything = mealie_plan.fetch_recipes(client) if target.is_mealie() else tools_meal_plan._fetch_all(client, "recipe")
    never = ", ".join(x for x in (household.get()["avoid"], params["guests_never"]) if x)
    if never:
        foods = cook_today.foods_by_recipe()
        rule = {"avoid": never, "dislikes": ""}
        everything = [r for r in everything if not household.avoided_in(foods.get(r["id"], []), r.get("name", ""), rule)]
    everything.sort(key=lambda r: -(r.get("rating") or 3))
    return everything[:MAX_CANDIDATES]


def _ask(job, payload) -> dict:
    def ask(system_prompt):
        out, usage = llm_provider.complete_tool_text(system_prompt, json.dumps(payload, ensure_ascii=False),
                                                     max_tokens=1200)
        job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
        job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
        return out, usage
    answer = json_answer.complete(ask, PROMPT.replace("{language}", settings.output_language))[0]
    return answer if isinstance(answer, dict) else {}


def _entry(course, recipe, reason) -> dict:
    return {"course": course, "reason": reason,
            "recipe": {"id": recipe["id"], "name": recipe.get("name", ""), "servings": recipe.get("servings") or None,
                       "minutes": tools_meal_plan._minutes(recipe) or None,
                       **({"slug": recipe["slug"]} if recipe.get("slug") else {})}}


def _plan(job, params, keep: list[dict]) -> None:
    """Fills the courses that aren't in `keep` (one AI call)."""
    with target.client().get_client() as client:
        candidates = _recipes(client, params)
    if not candidates:
        raise tandoor_client.TandoorError("No recipes to choose from.")
    by_id = {str(r["id"]): r for r in candidates}
    lines = tools_meal_plan._Lines(None)
    day = _date(params["date"])
    answer = _ask(job, {
        "occasion": params["occasion"], "date": f"{day.isoformat()} ({day.strftime('%A')})",
        "persons": params["persons"], "courses": params["courses"],
        "guests_never": [w.strip() for w in params["guests_never"].split(",") if w.strip()],
        "wishes": params["wishes"], "in_season": seasonal.display_names(seasonal.in_season(day.month)),
        "keep": [{"course": k["course"], "recipe_id": k["recipe"]["id"]} for k in keep],
        "recipes": [lines.line(r) for r in candidates],
    })
    kept = {k["course"]: k for k in keep}
    used = {str(k["recipe"]["id"]) for k in keep}
    menu = []
    for course in params["courses"]:
        if course in kept:
            menu.append(kept[course])
            continue
        pick = next((c for c in answer.get("courses") or [] if isinstance(c, dict) and c.get("course") == course
                     and str(c.get("recipe_id")) in by_id and str(c.get("recipe_id")) not in used), None)
        if pick:
            used.add(str(pick["recipe_id"]))
            menu.append(_entry(course, by_id[str(pick["recipe_id"])], str(pick.get("reason") or "").strip()))
    job.meta["menu"] = menu
    job.meta["note"] = str(answer.get("note") or "").strip()
    job.status = "ready"
    tool_jobs.save_tool_job(job)


def create(body: dict):
    if not llm_provider.is_configured():
        raise tandoor_client.TandoorError(llm_provider.missing_key_hint())
    job = tool_jobs.create_tool_job(TOOL)
    job.meta["params"] = params_from(body)
    _plan(job, job.meta["params"], [])
    return job


def _job(job_id):
    job = tool_jobs.get_tool_job(job_id)
    if job is None or job.tool != TOOL:
        raise tandoor_client.TandoorError("Menu not found.")
    return job


def reroll(job_id: str, course: str):
    """Another recipe for one course, the others stay."""
    job = _job(job_id)
    keep = [m for m in job.meta.get("menu", []) if m["course"] != course]
    old = next((m for m in job.meta.get("menu", []) if m["course"] == course), None)
    params = dict(job.meta["params"])
    if old:  # not the same one again
        params["wishes"] = (params["wishes"] + f" (not {old['recipe']['name']} for the {course})").strip()
    _plan(job, params, keep)
    return job


def shopping_list(job_id: str) -> dict:
    job = _job(job_id)
    from . import shopping_text
    result = shopping_text.collect_recipes([m["recipe"] for m in job.meta.get("menu", [])], job.meta["params"]["persons"])
    result["days"] = [job.meta["params"]["date"]]
    result["persons"] = job.meta["params"]["persons"]
    return result


def to_meal_plan(job_id: str, meal_type: dict, add_to_shopping: bool) -> int:
    """Every course into the meal plan on the menu's day, for the guests."""
    job = _job(job_id)
    params = job.meta["params"]
    done = 0
    with target.client().get_client() as client:
        for m in job.meta.get("menu", []):
            tools_meal_plan.create_plan_entry(client, m["recipe"], params["date"], meal_type, add_to_shopping,
                                              persons=params["persons"])
            done += 1
    job.meta["planned"] = True
    tool_jobs.save_tool_job(job)
    return done
