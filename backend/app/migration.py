"""Moving recipes between the two recipe managers: with Mealie as the
target, recipes come over from Tandoor, and the other way round. Needs both
configured (TANDOOR_URL/TOKEN and MEALIE_URL/TOKEN).

No AI involved: each recipe of the other manager is read as it is (title,
description, servings, times, tags, ingredients with their sections and
steps, source link, photo) and becomes an ExtractedRecipe of an import job.
The job then goes through the normal review - duplicate check (recipes that
were already moved are deselected), matching ingredients/units/tags with
the target's - and imports like any other. The cookbooks a recipe is in
(Tandoor: recipe books, Mealie: categories) are carried over too."""
from __future__ import annotations

import logging
import os
import uuid

import httpx

from . import mealie_client, tandoor_client, target
from .config import settings
from .mealie_plan import minutes as _mealie_minutes
from .schemas import ExtractedRecipe, Ingredient, Step
from .tandoor_helpers import fetch_recipe_overview

log = logging.getLogger("recipe-bridge")

MAX_IMAGE_BYTES = 15 * 1024 * 1024


def source_name() -> str | None:
    """The manager recipes can come from - the one that isn't the target -
    or None when it isn't configured."""
    if target.is_mealie():
        return "Tandoor" if settings.tandoor_url and settings.tandoor_token else None
    return "Mealie" if settings.mealie_url and settings.mealie_token else None


def _source():
    return tandoor_client if target.is_mealie() else mealie_client


def list_cookbooks() -> list[str]:
    with _source().get_client() as client:
        return _source().list_cookbooks(client)


# ---------- Tandoor -> ExtractedRecipe ----------

def _tandoor_list(client, path: str, params: dict | None = None) -> list[dict]:
    """All raw entries of a paged Tandoor list; None when it doesn't exist."""
    items, url, params = [], path, {"page_size": 200, **(params or {})}
    for _ in range(50):
        resp = client.get(url, params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("results", []) if isinstance(data, dict) else data)
        url, params = (data.get("next") if isinstance(data, dict) else None), None
        if not url:
            break
    return items


def _tandoor_books(client) -> dict:
    """recipe id -> names of the recipe books it is in."""
    books = {}
    try:
        for endpoint, entries in (("recipe-book", "recipe-book-entry"), ("cookbook", "cookbook-recipe")):
            found = _tandoor_list(client, f"/{endpoint}/")
            if found is None:
                continue
            for book in found:
                for row in _tandoor_list(client, f"/{entries}/", {"book": book["id"]}) or []:
                    in_book = row.get("book")
                    in_book = in_book.get("id") if isinstance(in_book, dict) else in_book
                    recipe = row.get("recipe")
                    recipe = recipe.get("id") if isinstance(recipe, dict) else recipe
                    if in_book == book["id"] and recipe is not None and book.get("name"):
                        books.setdefault(recipe, []).append(book["name"])
            break
    except httpx.HTTPError as exc:
        log.info("Recipe books not readable: %s", exc)
    return books


def _name(ref) -> str:
    return (ref or {}).get("name") or ""


def from_tandoor(recipe: dict) -> ExtractedRecipe:
    ingredients, steps = [], []
    group = None
    for step in recipe.get("steps") or []:
        n = len(steps)  # this step's position (steps without text or ingredients are left out)
        for ing in step.get("ingredients") or []:
            if ing.get("is_header"):
                group = (ing.get("note") or _name(ing.get("food"))).strip() or None
                continue
            food = _name(ing.get("food"))
            note = (ing.get("note") or "").strip() or None
            if not food and not note:
                continue
            amount = None if ing.get("no_amount") else ing.get("amount")
            ingredients.append(Ingredient(
                amount=float(amount) if amount else None, unit=_name(ing.get("unit")) or None,
                name=food or note, note=note if food else None, group=group, step_index=n))
        text = (step.get("instruction") or "").strip()
        if text or step.get("ingredients"):
            steps.append(Step(instruction=text, title=(step.get("name") or "").strip() or None,
                              time_minutes=step.get("time") or None))
    working, waiting = recipe.get("working_time") or 0, recipe.get("waiting_time") or 0
    return ExtractedRecipe(
        id=uuid.uuid4().hex[:10], title=recipe.get("name") or "?",
        description=(recipe.get("description") or "").strip() or None,
        servings=recipe.get("servings") or None,
        prep_time_minutes=working or None, cook_time_minutes=waiting or None,
        total_time_minutes=(working + waiting) or None,
        tags=[_name(k) for k in recipe.get("keywords") or [] if _name(k)],
        ingredients=ingredients, steps=steps,
        source_page_start=1, source_page_end=1,
        source_url=recipe.get("source_url") or None,
    )


# ---------- Mealie -> ExtractedRecipe ----------

def from_mealie(recipe: dict) -> ExtractedRecipe:
    instructions = recipe.get("recipeInstructions") or []
    first_step = {}
    for n, step in enumerate(instructions):
        for ref in step.get("ingredientReferences") or []:
            first_step.setdefault(ref.get("referenceId"), n)
    ingredients, group = [], None
    for row in recipe.get("recipeIngredient") or []:
        if (row.get("title") or "").strip():
            group = row["title"].strip()
        food = _name(row.get("food"))
        note = (row.get("note") or "").strip()
        if not food:  # an unparsed line: its text is the ingredient
            note = note or (row.get("originalText") or row.get("display") or "").strip()
        if not food and not note:
            continue
        amount = None if row.get("disableAmount") and not food else row.get("quantity")
        ingredients.append(Ingredient(
            amount=float(amount) if amount else None, unit=_name(row.get("unit")) or None,
            name=food or note, note=(note or None) if food else None, group=group,
            step_index=first_step.get(row.get("referenceId"), 0) if instructions else None))
    steps = [Step(instruction=(s.get("text") or "").strip(), title=(s.get("title") or "").strip() or None)
             for s in instructions]
    prep = _mealie_minutes({"totalTime": recipe.get("prepTime")})
    cook = _mealie_minutes({"totalTime": recipe.get("performTime") or recipe.get("cookTime")})
    total = _mealie_minutes({"totalTime": recipe.get("totalTime")}) or (prep + cook)
    servings = recipe.get("recipeServings") or recipe.get("recipeYieldQuantity") or None
    return ExtractedRecipe(
        id=uuid.uuid4().hex[:10], title=recipe.get("name") or "?",
        description=(recipe.get("description") or "").strip() or None,
        servings=round(servings) if servings else None,
        prep_time_minutes=prep or None, cook_time_minutes=cook or None, total_time_minutes=total or None,
        tags=[_name(t) for t in recipe.get("tags") or [] if _name(t)],
        ingredients=ingredients, steps=steps,
        source_page_start=1, source_page_end=1,
        source_url=recipe.get("orgURL") or None,
    )


# ---------- reading the whole collection ----------

def _download(client, url: str, path: str) -> bool:
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        log.info("Photo %s not loaded: %s", url, exc)
        return False
    if resp.status_code != 200 or not resp.content or len(resp.content) > MAX_IMAGE_BYTES:
        return False
    if not resp.headers.get("content-type", "image/").startswith("image/"):
        return False
    with open(path, "wb") as f:
        f.write(resp.content)
    return True


def read_recipes(cookbook: str | None, images_dir: str, on_progress=None):
    """Yields (ExtractedRecipe, image path or None) for every recipe of the
    source manager (or those in one cookbook)."""
    os.makedirs(images_dir, exist_ok=True)
    with _source().get_client() as client:
        if target.is_mealie():  # from Tandoor
            books = _tandoor_books(client)
            ids = [r["id"] for r in fetch_recipe_overview(client)]
            if cookbook:
                ids = [i for i in ids if cookbook in books.get(i, [])]
            for n, rid in enumerate(ids, 1):
                if on_progress:
                    on_progress(n, len(ids))
                resp = client.get(f"/recipe/{rid}/")
                if resp.status_code != 200:
                    log.info("Tandoor recipe %s not readable (%s)", rid, resp.status_code)
                    continue
                data = resp.json()
                recipe = from_tandoor(data)
                recipe.cookbooks = books.get(rid, [])
                image = None
                if data.get("image"):
                    image = os.path.join(images_dir, f"moved_{n}{_ext(data['image'])}")
                    image = image if _download(client, data["image"], image) else None
                yield recipe, image
        else:  # from Mealie
            items = mealie_client._paged(client, "/recipes")
            for n, item in enumerate(items, 1):
                if on_progress:
                    on_progress(n, len(items))
                resp = client.get(f"/recipes/{item['slug']}")
                if resp.status_code != 200:
                    continue
                data = resp.json()
                categories = [_name(c) for c in data.get("recipeCategory") or [] if _name(c)]
                if cookbook and cookbook not in categories:
                    continue
                recipe = from_mealie(data)
                recipe.cookbooks = categories
                image = None
                if data.get("image") and data.get("id"):
                    image = os.path.join(images_dir, f"moved_{n}.webp")
                    url = f"/media/recipes/{data['id']}/images/original.webp"
                    image = image if _download(client, url, image) else None
                yield recipe, image


def _ext(url: str) -> str:
    ext = os.path.splitext(url.split("?")[0])[1].lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"
