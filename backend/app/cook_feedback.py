""""How was it?" - after a planned meal, ask for a rating. The answer goes
into Tandoor as a cook log (recipe, servings, rating, date); Tandoor derives
a recipe's rating and "last cooked" from those, which the weekly plan then
uses (well rated and long not cooked first).

Asks about meal-plan entries of the last few days (up to today) that have a
recipe, that weren't answered here yet and for which Tandoor has no cook
log on or after that day (e.g. logged in Tandoor directly)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading

from . import mealie_plan, tandoor_client, target
from .config import settings

log = logging.getLogger("recipe-bridge")

LOOKBACK_DAYS = 7
_lock = threading.Lock()


def _path() -> str:
    return os.path.join(settings.data_dir, "cook_feedback.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _mark(plan_id, answer: str) -> None:
    with _lock:
        data = _load()
        data[str(plan_id)] = {"answer": answer, "at": dt.datetime.now().isoformat(timespec="seconds")}
        # Only the recent past matters - keep the file small.
        if len(data) > 500:
            data = dict(sorted(data.items(), key=lambda kv: kv[1]["at"])[-300:])
        os.makedirs(settings.data_dir, exist_ok=True)
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)


def _results(resp) -> list[dict]:
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", data) if isinstance(data, dict) else data


def _date(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def pending() -> list[dict]:
    today = dt.date.today()
    start = today - dt.timedelta(days=LOOKBACK_DAYS)
    answered = _load()
    if target.is_mealie():
        with target.client().get_client() as client:
            entries = mealie_plan.plan_entries(client, start, today)
        logs = [{"recipe": e["recipe"]["id"], "created_at": e["recipe"]["last_made"]}
                for e in entries if e.get("recipe") and e["recipe"].get("last_made")]
        return _open(entries, logs, answered, start, today)
    with tandoor_client.get_client() as client:
        entries = _results(client.get("/meal-plan/", params={
            "from_date": start.isoformat(), "to_date": today.isoformat(), "page_size": 200}))
        try:
            logs = _results(client.get("/cook-log/", params={"page_size": 200}))
        except Exception as exc:  # noqa: BLE001
            log.info("Could not read cook logs (%s) - asking about every planned meal", exc)
            logs = []
    return _open(entries, logs, answered, start, today)


def _open(entries, logs, answered, start, today) -> list[dict]:
    """Plan entries still to ask about: in the window, with a recipe, not
    answered here and not logged on or after that day."""
    logged = {}
    for entry in logs:
        rid = (entry.get("recipe") or {}).get("id") if isinstance(entry.get("recipe"), dict) else entry.get("recipe")
        day = _date(entry.get("created_at"))
        if rid and day:
            logged.setdefault(rid, []).append(day)
    result = []
    for e in entries:
        recipe, day = e.get("recipe") or {}, _date(e.get("from_date"))
        if not recipe.get("id") or not day or not (start <= day <= today) or str(e.get("id")) in answered:
            continue
        if any(d >= day for d in logged.get(recipe["id"], [])):
            continue  # already logged in Tandoor
        result.append({"plan_id": e["id"], "date": day.isoformat(), "recipe": {"id": recipe["id"], "name": recipe.get("name", "")},
                       "servings": e.get("servings") or 1, "meal_type": (e.get("meal_type") or {}).get("name")})
    result.sort(key=lambda x: x["date"], reverse=True)
    return result


def answer(plan_id, recipe_id, date: str, servings, rating) -> None:
    """rating 1-5 = cooked (creates the cook log), None = wasn't cooked."""
    if rating is not None and target.is_mealie():
        with target.client().get_client() as client:
            mealie_plan.rate(client, str(recipe_id), date, rating)
    elif rating is not None:
        payload = {"recipe": recipe_id, "servings": servings or 1, "rating": max(1, min(5, int(rating))),
                   "created_at": f"{date}T19:00:00"}
        with tandoor_client.get_client() as client:
            resp = client.post("/cook-log/", json=payload)
            if resp.status_code not in (200, 201):
                raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
    _mark(plan_id, "cooked" if rating is not None else "not_cooked")
