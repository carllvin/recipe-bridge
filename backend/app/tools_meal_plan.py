"""Weekly plan: the AI picks one recipe per day from the user's collection
(season, variety, the user's wishes; recipes cooked in the last two weeks
and days that already have a plan are left out). The first day is the
shopping day: recipes with quickly perishing ingredients (perishability.py)
come first, pantry and frozen dishes last, and what is already at home is
used up. A chat (chat()) then changes single days on request. Each day is a suggestion;
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

from . import cook_today, household, json_answer, llm_provider, mealie_plan, perishability, seasonal, tandoor_client, target, tool_jobs
from .config import settings
from .schemas import ToolJob, ToolSuggestion

log = logging.getLogger("recipe-bridge")

MAX_CANDIDATES = 300
RECENTLY_COOKED_DAYS = 14

# The household profile (household.py) as both prompts read it.
HOUSEHOLD = """HOUSEHOLD is {"persons": number or null, "never": [string], "dislikes": [string],
"fixed_days": {weekday: wish}}:
- NEVER pick a recipe whose title, tags or ingredients contain anything in
  "never" (allergies / intolerances - "Nüsse" also rules out Walnüsse,
  Haselnüsse, Nusskuchen ...)
- avoid recipes with "dislikes" unless the wishes ask for them
- on a weekday in "fixed_days" pick a recipe that fits that wish (e.g.
  "Friday": "Pizza" -> a pizza); this comes before the freshness order. If
  nothing fits, take the closest match and say so in "reason"
"""

# One recipe as the AI sees it (the "recipes" list of both prompts).
RECIPE_LINE = """Each RECIPE is one line:
"<id>|<title>|<tags>|<minutes>|<rating 1-5 or ->|<days since last cooked or never>|<its in-season ingredients>|<perishable level 3/2 and those ingredients, or ->|<its ingredients that are at home, or ->|<its fresh ingredients that usually leave a rest (cream, fresh herbs, feta ...), or ->"
"""

SYSTEM_PROMPT = """You plan meals for a home cook from their own recipe
collection. Language for "reason": {language}. You will receive a JSON
object: {"today": "YYYY-MM-DD", "meal": string, "days": ["YYYY-MM-DD (weekday)", ...],
"wishes": string, "household": HOUSEHOLD, "in_season": [string], "at_home": [string], "recipes": [RECIPE, ...]}
""" + RECIPE_LINE + HOUSEHOLD + """
The first day of "days" is the shopping day. Pick exactly one recipe per day
for that meal:
- follow the household rules above and the wishes (e.g. "2x vegetarian",
  "quick on weekdays")
- freshness: recipes with perishable level 3 (fresh fish and seafood, mince,
  leafy greens, fresh herbs, berries, mushrooms) on the first two days,
  level 2 (fresh meat and poultry, soft vegetables, fresh dairy) in the
  middle, and recipes without perishable ingredients (pantry, frozen,
  long-keeping) towards the end - this order comes before the weekday/weekend
  preference below
- prefer recipes that use what is already at home ("at_home"; the
  at-home field of a recipe lists which of those it uses) - perishable
  things at home early
- use up rests: when a recipe has an ingredient that usually leaves a rest
  (the last field - half a cup of cream, a bunch of herbs), plan another
  recipe with the same ingredient one to three days later, where it fits
  the other rules; mention it in "reason" (e.g. "uses the rest of the cream")
- prefer well-rated recipes (4-5) and ones not cooked for a long time;
  avoid recipes rated 1-2 unless the wishes ask for them; mix in one or
  two never-cooked recipes so new ones get tried
- prefer recipes that fit the current season - especially ones using the
  produce that is in season now ("in_season")
- vary it: no recipe twice, don't repeat the same kind of dish on
  consecutive days
- quicker recipes on weekdays, more elaborate ones on weekends, unless the
  wishes or the freshness order say otherwise
- only pick recipes that make sense as that meal

Respond with ONLY a JSON array (no explanation, no markdown fence), one
element per day: {"date": "YYYY-MM-DD", "recipe_id": <id>, "reason": <a few words>}
"""

CHAT_PROMPT = """You help a home cook adjust their weekly meal plan. Language
for "reply" and "reason": {language}. You will receive a JSON object:
{"meal": string, "plan": [{"date": "YYYY-MM-DD (weekday)", "recipe": {"id", "name"} or null,
"locked": bool}, ...], "wishes": string, "household": HOUSEHOLD, "at_home": [string],
"recipes": [RECIPE, ...], "history": [{"role": "user"|"assistant", "text": string}], "message": string}
""" + RECIPE_LINE + HOUSEHOLD + """
Do what "message" asks, e.g. "swap Thursday's dinner with a beef recipe",
"swap Monday and Wednesday", "something quicker on Tuesday", "no fish this
week". Change only the days the request is about - never "locked" days -
and use only recipes from "recipes" or ones already in the plan (to move
them). Keep the rules of a good plan: no recipe twice, perishable
ingredients early in the week, variety, the household rules ("never" even
when the message asks otherwise - then say why). If nothing fits, change
nothing and say so.

Respond with ONLY a JSON object (no explanation, no markdown fence):
{"reply": <one or two short sentences for the cook>,
 "changes": [{"date": "YYYY-MM-DD", "recipe_id": <id>, "reason": <a few words>}, ...]}
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
    # Allergies / intolerances of the household: out before the AI sees them.
    profile = household.get()
    if profile["avoid"]:
        foods = cook_today.foods_by_recipe()
        recipes = [r for r in recipes if not household.avoided_in(foods.get(r["id"], []), r.get("name", ""), profile)]
    # Well rated and long not cooked first (unrated counts as average); the
    # AI weighs it again, this only decides who makes the capped list.
    recipes.sort(key=lambda r: -((r.get("rating") or 3) + min(_days_since_cooked(r) or 60, 180) / 60))
    return recipes[:MAX_CANDIDATES]


def _days_since_cooked(recipe) -> int | None:
    cooked = _date(recipe.get("last_cooked"))
    return (dt.date.today() - cooked).days if cooked else None


class _Lines:
    """The recipe lines for the AI - with in-season, perishable and at-home
    ingredients from the recipe index (cook_today)."""

    def __init__(self, at_home_text):
        self.in_season = cook_today.seasonal_by_recipe()
        self.foods = cook_today.foods_by_recipe()
        self.have = cook_today.parse_have(at_home_text)

    def line(self, r) -> str:
        tags = ",".join(k.get("label") or k.get("name", "") for k in r.get("keywords") or [])
        rating = f"{r['rating']:g}" if r.get("rating") else "-"
        days = _days_since_cooked(r)
        foods = self.foods.get(r["id"], [])
        level, perishable = perishability.of_recipe(names[0] for names in foods)
        home = cook_today.at_home_in(self.have, foods) if self.have else []
        return (f"{r['id']}|{r.get('name', '')}|{tags}|{_minutes(r) or '?'}|{rating}|{'never' if days is None else days}"
                f"|{','.join(self.in_season.get(r['id'], []))}|{f'{level}:' + ','.join(perishable) if level else '-'}"
                f"|{','.join(home) or '-'}|{','.join(perishability.leftovers_of(n[0] for n in foods).values()) or '-'}")


def _add_usage(job, usage) -> None:
    job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
    job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0


def _suggestion(day: dt.date, recipe, reason, params) -> ToolSuggestion:
    meal_type = params["meal_type"]
    minutes = _minutes(recipe)
    return ToolSuggestion(
        id=uuid.uuid4().hex[:10], kind="meal_plan",
        summary=(f"{day.strftime('%a %d.%m.')} · {meal_type['name']}: {recipe.get('name', '')}"
                 + (f" ({minutes} min)" if minutes else "") + (f" – {reason}" if reason else "")),
        detail={"date": day.isoformat(), "recipe": {"id": recipe["id"], "name": recipe.get("name", ""),
                                                    "servings": recipe.get("servings") or None,
                                                    **({"slug": recipe["slug"]} if recipe.get("slug") else {})},
                "minutes": minutes or None, "reason": reason or None,
                "meal_type": meal_type, "add_to_shopping": bool(params.get("add_to_shopping"))},
    )


def _ask(job, prompt, payload, max_tokens):
    def ask(system_prompt):
        text_out, usage = llm_provider.complete_tool_text(system_prompt, json.dumps(payload, ensure_ascii=False),
                                                          max_tokens=max_tokens)
        _add_usage(job, usage)
        return text_out, usage
    return json_answer.complete(ask, prompt.replace("{language}", settings.output_language))[0]


def _pick(job, candidates, days, params, exclude_ids=frozenset()) -> list[ToolSuggestion]:
    """One AI call picking a recipe for each of `days`; returns validated
    suggestions (unknown recipes, repeats and wrong dates are dropped)."""
    pool = [r for r in candidates if r["id"] not in exclude_ids]
    by_id = {str(r["id"]): r for r in pool}
    lines = _Lines(params.get("at_home"))
    answers = _ask(job, SYSTEM_PROMPT, {
        "today": dt.date.today().isoformat(),
        "meal": params["meal_type"]["name"],
        "days": [f"{d.isoformat()} ({d.strftime('%A')})" for d in days],
        "wishes": params.get("wishes") or "",
        "household": household.for_ai(),
        "in_season": seasonal.display_names(seasonal.in_season()),
        "at_home": [h.strip() for h in (params.get("at_home") or "").replace(";", ",").split(",") if h.strip()],
        "recipes": [lines.line(r) for r in pool],
    }, 60 * len(days) + 200)

    suggestions, used = [], set()
    for answer in answers if isinstance(answers, list) else []:
        day = _date(answer.get("date")) if isinstance(answer, dict) else None
        recipe = by_id.get(str(answer.get("recipe_id"))) if day else None
        if day not in days or recipe is None or recipe["id"] in used:
            continue  # invalid date, unknown recipe or a repeat
        used.add(recipe["id"])
        suggestions.append(_suggestion(day, recipe, str(answer.get("reason") or "").strip(), params))
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
        mark_shared(job)
        job.status = "ready"
        job.progress_label = None
        tool_jobs.save_tool_job(job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Meal plan scan failed for job %s", job_id)
        job.status = "error"
        job.error = str(exc)
        tool_jobs.save_tool_job(job)


SHARE_WITHIN_DAYS = 3


def mark_shared(job) -> None:
    """Each planned day: its ingredients that usually leave a rest and the
    other days (within SHARE_WITHIN_DAYS) that use them too - shown on the
    day cards as "rest of the cream: Thursday"."""
    foods = cook_today.foods_by_recipe()
    active = [s for s in job.suggestions if s.status != "skipped"]
    rests = {s.id: perishability.leftovers_of(n[0] for n in foods.get(s.detail["recipe"]["id"], [])) for s in active}
    for s in active:
        day = _date(s.detail["date"])
        shared = []
        for stem, name in rests[s.id].items():
            others = sorted(o.detail["date"] for o in active
                            if o is not s and stem in rests[o.id] and abs((_date(o.detail["date"]) - day).days) <= SHARE_WITHIN_DAYS)
            if others:
                shared.append({"name": name, "days": others})
        s.detail["shared"] = shared


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
    mark_shared(job)
    tool_jobs.save_tool_job(job)
    return job


CHAT_HISTORY = 6  # earlier messages the AI sees


def chat(job_id: str, message: str) -> ToolJob:
    """Changes the finished plan as asked in `message` (one AI call): days
    that aren't in the meal plan yet get another recipe, or two days swap.
    The conversation is kept in job.meta["chat"]."""
    job = tool_jobs.get_tool_job(job_id)
    if job is None or job.tool != "meal_plan" or job.status != "ready":
        raise tandoor_client.TandoorError("There's no finished plan to change.")
    params = job.meta.get("params", {})
    days = [_date(d) for d in job.meta.get("days", [])]
    taken = set(job.meta.get("taken_days", []))
    current = {}
    for s in job.suggestions:
        if s.status != "skipped":
            current[s.detail["date"]] = s
    with target.client().get_client() as client:
        candidates = _candidates(client)
    by_id = {str(r["id"]): r for r in candidates}
    for s in current.values():  # recipes already in the plan can move even when not in the list
        by_id.setdefault(str(s.detail["recipe"]["id"]), {"id": s.detail["recipe"]["id"], "name": s.detail["recipe"]["name"],
                                                          "working_time": s.detail.get("minutes") or 0})

    def locked(day):
        return day.isoformat() in taken or getattr(current.get(day.isoformat()), "status", None) == "applied"
    lines = _Lines(params.get("at_home"))
    history = job.meta.get("chat", [])
    answer = _ask(job, CHAT_PROMPT, {
        "meal": params["meal_type"]["name"],
        "plan": [{"date": f"{d.isoformat()} ({d.strftime('%A')})",
                  "recipe": ({"id": current[d.isoformat()].detail["recipe"]["id"],
                              "name": current[d.isoformat()].detail["recipe"]["name"]} if d.isoformat() in current else None),
                  "locked": locked(d)} for d in days],
        "wishes": params.get("wishes") or "",
        "household": household.for_ai(),
        "at_home": [h.strip() for h in (params.get("at_home") or "").replace(";", ",").split(",") if h.strip()],
        "recipes": [lines.line(r) for r in candidates],
        "history": history[-CHAT_HISTORY:],
        "message": message,
    }, 1200)
    answer = answer if isinstance(answer, dict) else {}

    changed, changed_days = [], set()

    def put(day, recipe, reason):
        new = _suggestion(day, recipe, reason, params)
        job.suggestions = [s for s in job.suggestions if s.detail.get("date") != day.isoformat()] + [new]
        current[day.isoformat()] = new
        changed_days.add(day.isoformat())
        changed.append(new.id)

    valid = []
    for change in answer.get("changes") or []:
        day = _date(change.get("date")) if isinstance(change, dict) else None
        recipe = by_id.get(str(change.get("recipe_id"))) if day else None
        if day in days and recipe is not None and not locked(day):
            valid.append((day, recipe, str(change.get("reason") or "").strip()))
    targets = {d.isoformat() for d, _r, _x in valid}
    for day, recipe, reason in valid:
        old = current.get(day.isoformat())
        if old is not None and str(old.detail["recipe"]["id"]) == str(recipe["id"]):
            continue  # unchanged
        # The recipe is already on another open day the answer doesn't touch:
        # swap the two, so no recipe is planned twice.
        other = next((d for d, s in current.items() if d != day.isoformat() and d not in targets and d not in changed_days
                      and s.status != "applied" and str(s.detail["recipe"]["id"]) == str(recipe["id"])), None)
        put(day, recipe, reason)
        if other and old is not None:
            put(_date(other), by_id.get(str(old.detail["recipe"]["id"]), old.detail["recipe"]), old.detail.get("reason") or "")
    job.suggestions.sort(key=lambda s: s.detail["date"])
    mark_shared(job)
    reply = str(answer.get("reply") or "").strip()
    job.meta["chat"] = history + [{"role": "user", "text": message},
                                  {"role": "assistant", "text": reply or ("✓" if changed else "–"), "changed": len(changed)}]
    job.meta["last_changed"] = changed
    tool_jobs.save_tool_job(job)
    return job


def create_plan_entry(client, recipe: dict, date: str, meal_type: dict, add_to_shopping: bool) -> dict:
    """Creates one entry in Tandoor's meal plan with the household's
    number of persons as servings (else the recipe's own) - the shopping
    list scales the amounts to it. Returns the created entry. With Mealie:
    its meal plan and shopping list (mealie_plan)."""
    if target.is_mealie():
        return mealie_plan.create_entry(client, recipe, date, meal_type, add_to_shopping)
    resp = client.get(f"/recipe/{recipe['id']}/")
    resp.raise_for_status()
    payload = {
        "title": "",
        "recipe": {"id": recipe["id"], "name": recipe.get("name", "")},
        "servings": household.servings(resp.json().get("servings")) or 1,
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
