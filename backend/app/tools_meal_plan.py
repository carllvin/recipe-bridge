"""Weekly plan: the AI picks one recipe per day from the user's collection
(season, variety, the user's wishes; recipes cooked in the last two weeks
and days that already have a plan are left out). Each day is a suggestion;
applying it creates the entry in Tandoor's meal plan and - if wanted - puts
the recipe's ingredients on Tandoor's shopping list (which groups them by
the supermarket categories). With Mealie the same via mealie_plan.

Uses the cheaper tools model: the input is a compact one-line-per-recipe
list, the answer one line per day."""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

from . import cook_today, llm_provider, mealie_plan, seasonal, tandoor_client, target, tool_jobs
from .config import settings
from .schemas import ToolJob, ToolSuggestion

log = logging.getLogger("recipe-bridge")

MAX_CANDIDATES = 300
RECENTLY_COOKED_DAYS = 14

SYSTEM_PROMPT = """You plan meals for a home cook from their own recipe
collection. Language for "reason": {language}. You will receive a JSON
object: {"today": "YYYY-MM-DD", "meal": string, "days": ["YYYY-MM-DD (weekday)", ...],
"wishes": string, "in_season": [string], "recipes": ["<id>|<title>|<tags>|<minutes>|<rating 1-5 or ->|<days since last cooked or never>|<its in-season ingredients>", ...]}

Pick exactly one recipe per day for that meal:
- follow the wishes (e.g. "2x vegetarian", "quick on weekdays")
- prefer well-rated recipes (4-5) and ones not cooked for a long time;
  avoid recipes rated 1-2 unless the wishes ask for them; mix in one or
  two never-cooked recipes so new ones get tried
- prefer recipes that fit the current season - especially ones using the
  produce that is in season now ("in_season"; the last field of a recipe
  lists its in-season ingredients)
- vary it: no recipe twice, don't repeat the same kind of dish on
  consecutive days
- quicker recipes on weekdays, more elaborate ones on weekends, unless the
  wishes say otherwise
- only pick recipes that make sense as that meal

Respond with ONLY a JSON array (no explanation, no markdown fence), one
element per day: {"date": "YYYY-MM-DD", "recipe_id": <id>, "reason": <a few words>}
"""


def _fetch_all(client, endpoint, params=None) -> list[dict]:
    items, url, first = [], f"/{endpoint}/", {"page_size": 200, **(params or {})}
    for _ in range(100):
        resp = client.get(url, params=first)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("results", data) if isinstance(data, dict) else data)
        url = data.get("next") if isinstance(data, dict) else None
        if not url:
            break
        first = None
    return items


def options() -> dict:
    """Meal types for the form (breakfast, dinner, ... as set up in Tandoor;
    Mealie's are fixed)."""
    if target.is_mealie():
        return {"meal_types": mealie_plan.meal_types()}
    with tandoor_client.get_client() as client:
        types = _fetch_all(client, "meal-type")
    return {"meal_types": [{"id": t["id"], "name": t["name"]} for t in types]}


def _date(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _minutes(recipe) -> int:
    return (recipe.get("working_time") or 0) + (recipe.get("waiting_time") or 0)


def _candidates(client) -> list[dict]:
    """Recipes that may be planned: not cooked in the last two weeks, best
    rated first, capped to keep the prompt small."""
    cutoff = dt.date.today() - dt.timedelta(days=RECENTLY_COOKED_DAYS)
    everything = mealie_plan.fetch_recipes(client) if target.is_mealie() else _fetch_all(client, "recipe")
    recipes = [r for r in everything
               if not (_date(r.get("last_cooked")) and _date(r.get("last_cooked")) >= cutoff)]
    # Well rated and long not cooked first (unrated counts as average); the
    # AI weighs it again, this only decides who makes the capped list.
    recipes.sort(key=lambda r: -((r.get("rating") or 3) + min(_days_since_cooked(r) or 60, 180) / 60))
    return recipes[:MAX_CANDIDATES]


def _days_since_cooked(recipe) -> int | None:
    cooked = _date(recipe.get("last_cooked"))
    return (dt.date.today() - cooked).days if cooked else None


def _pick(job, candidates, days, params, exclude_ids=frozenset()) -> list[ToolSuggestion]:
    """One AI call picking a recipe for each of `days`; returns validated
    suggestions (unknown recipes, repeats and wrong dates are dropped)."""
    meal_type = params["meal_type"]
    pool = [r for r in candidates if r["id"] not in exclude_ids]
    by_id = {r["id"]: r for r in pool}
    in_season = cook_today.seasonal_by_recipe()

    def line(r):
        tags = ",".join(k.get("label") or k.get("name", "") for k in r.get("keywords") or [])
        rating = f"{r['rating']:g}" if r.get("rating") else "-"
        days = _days_since_cooked(r)
        return (f"{r['id']}|{r.get('name', '')}|{tags}|{_minutes(r) or '?'}|{rating}|{'never' if days is None else days}"
                f"|{','.join(in_season.get(r['id'], []))}")
    lines = [line(r) for r in pool]
    text_out, usage = llm_provider.complete_tool_text(
        SYSTEM_PROMPT.replace("{language}", settings.output_language),
        json.dumps({
            "today": dt.date.today().isoformat(),
            "meal": meal_type["name"],
            "days": [f"{d.isoformat()} ({d.strftime('%A')})" for d in days],
            "wishes": params.get("wishes") or "",
            "in_season": seasonal.display_names(seasonal.in_season()),
            "recipes": lines,
        }, ensure_ascii=False),
        max_tokens=60 * len(days) + 200,
    )
    job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
    job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
    text_out = text_out.strip().strip("`")
    if text_out.startswith("json"):
        text_out = text_out[4:]
    answers = json.loads(text_out)

    suggestions, used = [], set()
    for answer in answers if isinstance(answers, list) else []:
        day = _date(answer.get("date")) if isinstance(answer, dict) else None
        recipe = by_id.get(answer.get("recipe_id")) if day else None
        if day not in days or recipe is None or recipe["id"] in used:
            continue  # invalid date, unknown recipe or a repeat
        used.add(recipe["id"])
        minutes = _minutes(recipe)
        reason = str(answer.get("reason") or "").strip()
        suggestions.append(ToolSuggestion(
            id=uuid.uuid4().hex[:10], kind="meal_plan",
            summary=(f"{day.strftime('%a %d.%m.')} · {meal_type['name']}: {recipe.get('name', '')}"
                     + (f" ({minutes} min)" if minutes else "") + (f" – {reason}" if reason else "")),
            detail={"date": day.isoformat(), "recipe": {"id": recipe["id"], "name": recipe.get("name", "")},
                    "minutes": minutes or None, "reason": reason or None,
                    "meal_type": meal_type, "add_to_shopping": bool(params.get("add_to_shopping"))},
        ))
    return suggestions


def run_scan(job_id: str) -> None:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        return
    params = job.meta.get("params", {})
    try:
        if not llm_provider.is_configured():
            job.status = "error"
            job.error = llm_provider.missing_key_hint()
            tool_jobs.save_tool_job(job)
            return
        start = _date(params.get("start_date")) or dt.date.today()
        days = [start + dt.timedelta(days=i) for i in range(max(1, min(int(params.get("days") or 7), 14)))]
        meal_type = params["meal_type"]

        with target.client().get_client() as client:
            job.progress_label = "Loading recipes and the existing meal plan..."
            tool_jobs.save_tool_job(job)
            candidates = _candidates(client)
            try:
                planned = (mealie_plan.plan_entries(client, days[0], days[-1]) if target.is_mealie() else
                           _fetch_all(client, "meal-plan", {"from_date": days[0].isoformat(), "to_date": days[-1].isoformat()}))
            except Exception as exc:  # noqa: BLE001
                log.info("Could not read the existing meal plan (%s) - not skipping any days", exc)
                planned = []

        taken = {_date(p.get("from_date")) for p in planned if (p.get("meal_type") or {}).get("id") == meal_type["id"]}
        free_days = [d for d in days if d not in taken]
        # For the week view: every day of the range, and which were already planned.
        job.meta["days"] = [d.isoformat() for d in days]
        job.meta["taken_days"] = sorted(d.isoformat() for d in taken if d in days)
        job.progress_total = len(free_days)
        job.progress_label = "Planning..."
        tool_jobs.save_tool_job(job)

        suggestions = _pick(job, candidates, free_days, params) if free_days else []
        suggestions.sort(key=lambda s: s.detail["date"])
        job.suggestions = suggestions
        job.status = "ready"
        job.progress_label = None
        tool_jobs.save_tool_job(job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Meal plan scan failed for job %s", job_id)
        job.status = "error"
        job.error = str(exc)
        tool_jobs.save_tool_job(job)


def reroll_day(job_id: str, date: str) -> ToolJob:
    """Picks a different recipe for one day of a finished plan - replaces
    that day's pending suggestion (or fills a day that had none). Recipes
    already used elsewhere in this plan are excluded."""
    job = tool_jobs.get_tool_job(job_id)
    if job is None or job.tool != "meal_plan":
        raise tandoor_client.TandoorError("Job not found.")
    day = _date(date)
    if day is None or date not in job.meta.get("days", []) or date in job.meta.get("taken_days", []):
        raise tandoor_client.TandoorError("That day can't be planned here.")
    current = [s for s in job.suggestions if s.detail.get("date") == date]
    if any(s.status == "applied" for s in current):
        raise tandoor_client.TandoorError("That day is already in the meal plan.")
    exclude = {s.detail["recipe"]["id"] for s in job.suggestions if s.status != "skipped"}
    with target.client().get_client() as client:
        candidates = _candidates(client)
    picked = _pick(job, candidates, [day], job.meta.get("params", {}), exclude_ids=exclude)
    if not picked:
        raise tandoor_client.TandoorError("No other matching recipe found for that day.")
    job.suggestions = [s for s in job.suggestions if s.detail.get("date") != date] + picked
    job.suggestions.sort(key=lambda s: s.detail["date"])
    tool_jobs.save_tool_job(job)
    return job


def create_plan_entry(client, recipe: dict, date: str, meal_type: dict, add_to_shopping: bool) -> dict:
    """Creates one entry in Tandoor's meal plan with the recipe's own
    servings - so the shopping list gets exactly the amounts written in the
    recipe. Returns the created entry. With Mealie: its meal plan and
    shopping list (mealie_plan)."""
    if target.is_mealie():
        return mealie_plan.create_entry(client, recipe, date, meal_type, add_to_shopping)
    resp = client.get(f"/recipe/{recipe['id']}/")
    resp.raise_for_status()
    payload = {
        "title": "",
        "recipe": {"id": recipe["id"], "name": recipe.get("name", "")},
        "servings": resp.json().get("servings") or 1,
        "note": "",
        "from_date": date,
        "to_date": date,
        "meal_type": meal_type,
        "shared": [],
        # Tandoor adds the recipe's ingredients to the shopping list when a
        # plan entry is created with this flag.
        "addshopping": add_to_shopping,
    }
    resp = client.post("/meal-plan/", json=payload)
    if resp.status_code == 400 and "date" in resp.text.lower():
        # Newer Tandoor versions store plan dates as date-times.
        payload["from_date"] = payload["to_date"] = f"{date}T00:00:00"
        resp = client.post("/meal-plan/", json=payload)
    if resp.status_code not in (200, 201):
        raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
    return resp.json()


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
        with target.client().get_client() as client:
            create_plan_entry(client, d["recipe"], d["date"], d["meal_type"], d["add_to_shopping"])
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status = "error"
        suggestion.error = str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion
