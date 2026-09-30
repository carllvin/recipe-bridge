"""A shopping list to share (messenger, notes app): the ingredients of the
weekly plan's days added up - same ingredient and unit once, scaled to the
household's persons - and grouped by supermarket aisle (Tandoor: the
ingredient's supermarket category, Mealie: its label). Pantry staples (salt,
oil, ...) and what is already at home come in a separate "check" group.

Returns structure, not text: the page words it in the UI language."""
from __future__ import annotations

import re

from . import cook_today, household, mealie_client, target, tandoor_client, tool_jobs


def _recipe(client, recipe: dict) -> dict | None:
    """The recipe's detail in Tandoor's shape (steps -> ingredients)."""
    if target.is_mealie():
        from .mealie_tools import view
        resp = client.get(f"/recipes/{recipe.get('slug') or recipe['id']}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        shaped = view(data, for_tags=True)  # unparsed lines (no linked food) count with their text
        labels = {}
        for row in data.get("recipeIngredient") or []:
            food = row.get("food") or {}
            if food.get("id"):
                labels[food["id"]] = (food.get("label") or {}).get("name")
        for step in shaped["steps"]:
            for ing in step["ingredients"]:
                if ing.get("food"):
                    ing["food"]["supermarket_category"] = {"name": labels.get(ing["food"].get("id"))}
        shaped["servings"] = data.get("recipeServings") or data.get("recipeYieldQuantity") or 0
        return shaped
    resp = client.get(f"/recipe/{recipe['id']}/")
    return resp.json() if resp.status_code == 200 else None


def _key(text) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()


def collect(job_id: str) -> dict:
    """{"days": [date, ...], "groups": [{"category", "items": [{"name", "amount", "unit"}]}],
    "check": [...same items...], "missing": [recipe names that couldn't be read]}"""
    job = tool_jobs.get_tool_job(job_id)
    if job is None or job.tool != "meal_plan":
        raise tandoor_client.TandoorError("Job not found.")
    days = [s for s in job.suggestions if s.status != "skipped"]
    have = cook_today.parse_have(job.meta.get("params", {}).get("at_home"))
    persons = household.get()["persons"]
    items, missing = {}, []
    with target.client().get_client() as client:
        for s in days:
            recipe = _recipe(client, s.detail["recipe"])
            if recipe is None:
                missing.append(s.detail["recipe"].get("name", ""))
                continue
            factor = persons / recipe["servings"] if persons and recipe.get("servings") else 1
            for step in recipe.get("steps") or []:
                for ing in step.get("ingredients") or []:
                    food = ing.get("food") or {}
                    if ing.get("is_header") or not food.get("name"):
                        continue
                    unit = (ing.get("unit") or {}).get("name") or ""
                    amount = None if ing.get("no_amount") or not ing.get("amount") else float(ing["amount"]) * factor
                    key = (_key(food["name"]), _key(unit))
                    entry = items.setdefault(key, {
                        "name": food["name"], "plural": food.get("plural_name") or "", "unit": unit, "amount": 0.0,
                        "some_without_amount": False, "recipes": [],
                        "category": ((food.get("supermarket_category") or {}).get("name") or "").strip(),
                    })
                    if amount is None:
                        entry["some_without_amount"] = True
                    else:
                        entry["amount"] += amount
                    if s.detail["recipe"].get("name") not in entry["recipes"]:
                        entry["recipes"].append(s.detail["recipe"].get("name"))

    groups, check = {}, []
    for entry in items.values():
        names = [entry["name"], entry["plural"]]
        item = {"name": entry["plural"] if entry["plural"] and entry["amount"] > 1 and not entry["unit"] else entry["name"],
                "amount": round(entry["amount"], 2) or None, "unit": entry["unit"] or None,
                "recipes": entry["recipes"]}
        if cook_today._is_staple(names) or any(cook_today._matches(h, names) for h in have):
            check.append(item)
        else:
            groups.setdefault(entry["category"], []).append(item)
    ordered = sorted(groups, key=lambda c: (c == "", c.casefold()))  # without a category last
    return {
        "days": sorted(s.detail["date"] for s in days),
        "groups": [{"category": c or None, "items": sorted(groups[c], key=lambda i: i["name"].casefold())} for c in ordered],
        "check": sorted(check, key=lambda i: i["name"].casefold()),
        "missing": missing,
        "persons": persons or None,
    }
