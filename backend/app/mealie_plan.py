"""Planning with Mealie (RECIPE_MANAGER=mealie): the weekly plan, "What can
I cook today?" and "How was it?" use the same code as with Tandoor - this
module turns Mealie's data into the Tandoor shapes that code reads and
writes the results back the Mealie way:

- meal types are Mealie's fixed entry types (breakfast, lunch, dinner, ...);
- a plan entry goes to /api/households/mealplans; its ingredients (if
  wanted) to the first shopping list (/api/households/shopping/lists/<id>/recipe),
  which is created when there is none;
- Mealie has no cook log: "How was it?" sets the user's rating
  (/api/users/<id>/ratings/<recipe>) and the recipe's "last made"
  (/api/recipes/<recipe>/last-made) - the two things the weekly plan reads.

Recipe ids are Mealie's uuids (its API takes a slug or an id)."""
from __future__ import annotations

import datetime as dt
import re

from . import mealie_client
from .config import settings
from .tandoor_client import TandoorError

ENTRY_TYPES = ["breakfast", "lunch", "dinner", "side", "snack", "dessert", "drink"]
ENTRY_NAMES = {
    "de": {"breakfast": "Frühstück", "lunch": "Mittagessen", "dinner": "Abendessen", "side": "Beilage",
           "snack": "Snack", "dessert": "Dessert", "drink": "Getränk"},
    "fr": {"breakfast": "Petit-déjeuner", "lunch": "Déjeuner", "dinner": "Dîner", "side": "Accompagnement",
           "snack": "En-cas", "dessert": "Dessert", "drink": "Boisson"},
    "it": {"breakfast": "Colazione", "lunch": "Pranzo", "dinner": "Cena", "side": "Contorno",
           "snack": "Spuntino", "dessert": "Dessert", "drink": "Bevanda"},
    "es": {"breakfast": "Desayuno", "lunch": "Almuerzo", "dinner": "Cena", "side": "Guarnición",
           "snack": "Tentempié", "dessert": "Postre", "drink": "Bebida"},
}
LANGUAGE_CODES = {"deutsch": "de", "german": "de", "français": "fr", "french": "fr", "italiano": "it",
                  "italian": "it", "español": "es", "spanish": "es"}
DEFAULT_SHOPPING_LIST = "Einkaufsliste"


def meal_types() -> list[dict]:
    lang = settings.output_language or ""
    names = ENTRY_NAMES.get(LANGUAGE_CODES.get(lang.strip().casefold(), lang[:2].casefold()), {})
    return [{"id": t, "name": names.get(t, t.capitalize())} for t in ENTRY_TYPES]


def minutes(recipe) -> int:
    """Total time in minutes from Mealie's free-text times ("45 min",
    "1 hour 30 minutes", "PT1H30M", "1:30") - total, else prep + cook."""
    def parse(text) -> int:
        text = str(text or "").strip().casefold()
        if not text:
            return 0
        if m := re.fullmatch(r"(\d+):(\d{1,2})", text):
            return int(m.group(1)) * 60 + int(m.group(2))
        hours = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:h\b|hours?|std|stunden?|heures?|ore|horas?)", text)
        mins = re.search(r"(\d+)\s*(?:m\b|min)", text)
        if m := re.fullmatch(r"p(?:t)?(?:(\d+)h)?(?:(\d+)m)?(?:\d+s)?", text):
            return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
        if hours or mins:
            return round(float(hours.group(1).replace(",", ".")) * 60 if hours else 0) + (int(mins.group(1)) if mins else 0)
        return int(text) if text.isdigit() else 0
    total = parse(recipe.get("totalTime"))
    return total or parse(recipe.get("prepTime")) + parse(recipe.get("performTime") or recipe.get("cookTime"))


def as_tandoor(recipe: dict) -> dict:
    """The fields the planning code reads, in Tandoor's names."""
    ingredients = [{"food": {"id": (i.get("food") or {}).get("id"), "name": (i.get("food") or {}).get("name", ""),
                             "plural_name": (i.get("food") or {}).get("pluralName") or ""}}
                   for i in recipe.get("recipeIngredient") or [] if (i.get("food") or {}).get("id")]
    return {
        "id": recipe.get("id"), "slug": recipe.get("slug"), "name": recipe.get("name", ""),
        "keywords": [{"name": t.get("name", "")} for t in recipe.get("tags") or []],
        "working_time": minutes(recipe), "waiting_time": 0,
        "rating": recipe.get("rating") or None, "last_cooked": recipe.get("lastMade"),
        "servings": recipe.get("recipeServings") or recipe.get("recipeYieldQuantity") or None,
        "steps": [{"ingredients": ingredients}],
    }


def fetch_recipes(client) -> list[dict]:
    """Every recipe (list view: tags, times, rating, last made) - enough
    for the weekly plan."""
    return [as_tandoor(r) for r in mealie_client._paged(client, "/recipes")]


def plan_entries(client, start: dt.date, end: dt.date) -> list[dict]:
    """Meal-plan entries between the two days, in Tandoor's shape."""
    items = []
    for page in range(1, mealie_client.MAX_PAGES + 1):
        resp = client.get("/households/mealplans", params={
            "start_date": start.isoformat(), "end_date": end.isoformat(), "page": page, "perPage": 200})
        data = mealie_client._check(resp, "loading the meal plan").json()
        items.extend(data.get("items", []))
        if page >= (data.get("total_pages") or data.get("totalPages") or 1):
            break
    names = {m["id"]: m["name"] for m in meal_types()}
    result = []
    for e in items:
        recipe = e.get("recipe") or {}
        result.append({
            "id": e.get("id"), "from_date": e.get("date"),
            "meal_type": {"id": e.get("entryType"), "name": names.get(e.get("entryType"), e.get("entryType"))},
            "recipe": {"id": e.get("recipeId") or recipe.get("id"), "name": recipe.get("name") or e.get("title", ""),
                       "last_made": recipe.get("lastMade")} if (e.get("recipeId") or recipe.get("id")) else None,
            "servings": recipe.get("recipeServings") or 1,
        })
    return result


def create_entry(client, recipe: dict, date: str, meal_type: dict, add_to_shopping: bool) -> dict:
    entry_type = meal_type.get("id") if meal_type.get("id") in ENTRY_TYPES else "dinner"
    entry = mealie_client._check(client.post("/households/mealplans", json={
        "date": date[:10], "entryType": entry_type, "recipeId": recipe["id"], "title": "", "text": ""}),
        "the meal-plan entry").json()
    if add_to_shopping:
        add_to_shopping_list(client, recipe["id"])
    return entry


def add_to_shopping_list(client, recipe_id: str) -> None:
    """Adds the recipe's ingredients to the first shopping list (Mealie
    merges them with what is already there)."""
    lists = mealie_client._paged(client, "/households/shopping/lists")
    if lists:
        list_id = lists[0]["id"]
    else:
        list_id = mealie_client._check(client.post("/households/shopping/lists", json={"name": DEFAULT_SHOPPING_LIST}),
                                       "creating a shopping list").json()["id"]
    resp = client.post(f"/households/shopping/lists/{list_id}/recipe",
                       json=[{"recipeId": recipe_id, "recipeIncrementQuantity": 1}])
    if resp.status_code in (404, 405, 422):  # Mealie before the bulk endpoint
        resp = client.post(f"/households/shopping/lists/{list_id}/recipe/{recipe_id}", json={"recipeIncrementQuantity": 1})
    mealie_client._check(resp, "adding the ingredients to the shopping list")


def rate(client, recipe_id: str, date: str, rating: int) -> None:
    """"How was it?" with Mealie: the user's rating and the recipe's
    last-made date."""
    user = mealie_client._check(client.get("/users/self"), "reading the user").json()
    if not user.get("id"):
        raise TandoorError("Mealie did not say which user the token belongs to.")
    mealie_client._check(client.post(f"/users/{user['id']}/ratings/{recipe_id}",
                                     json={"rating": max(1, min(5, int(rating)))}), "saving the rating")
    mealie_client._check(client.patch(f"/recipes/{recipe_id}/last-made", json={"timestamp": f"{date[:10]}T19:00:00"}),
                         "saving the last-made date")
