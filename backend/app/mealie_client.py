"""Mealie as the target of an import (RECIPE_MANAGER=mealie) - the same
functions the import uses from tandoor_client, for Mealie's REST API:

- ingredients, units and tags are looked up by name and created when
  missing (/api/foods, /api/units, /api/organizers/tags), then linked in the
  recipe by id - like tandoor_client does, so an existing "Zwiebel" is reused;
- a recipe is created by name (POST /api/recipes -> its slug) and then
  filled in (PATCH /api/recipes/<slug>); steps point to their ingredients
  via reference ids (the step assignment from the review);
- the cookbook name becomes a Mealie category (Mealie's cookbooks are saved
  filters, e.g. on a category);
- the photo goes to PUT /api/recipes/<slug>/image.

Maintenance (mealie_maintenance) and planning (mealie_plan) build on this
client for the parts that exist in Mealie."""
from __future__ import annotations

import logging
import os
import uuid

import httpx

from .config import settings
from .schemas import ExtractedRecipe
from .tandoor_client import TandoorError  # the import catches this error type for any target

log = logging.getLogger("recipe-bridge")

PAGE_SIZE = 200
MAX_PAGES = 100
NAME_MAX_LENGTH = 250


def get_client() -> httpx.Client:
    if not settings.mealie_url or not settings.mealie_token:
        raise TandoorError("MEALIE_URL / MEALIE_TOKEN are not configured (see .env).")
    return httpx.Client(
        base_url=f"{settings.mealie_url.rstrip('/')}/api",
        headers={"Authorization": f"Bearer {settings.mealie_token}", "Accept": "application/json"},
        timeout=60.0,
    )


def _check(resp: httpx.Response, what: str) -> httpx.Response:
    if resp.status_code >= 400:
        raise TandoorError(f"Mealie rejected {what} ({resp.status_code}): {resp.text[:400]}")
    return resp


def _paged(client, path: str) -> list[dict]:
    items = []
    for page in range(1, MAX_PAGES + 1):
        try:
            resp = client.get(path, params={"page": page, "perPage": PAGE_SIZE})
        except httpx.HTTPError as exc:
            raise TandoorError(f"Network error loading {path}: {exc}") from exc
        data = _check(resp, f"loading {path}").json()
        items.extend(data.get("items", []))
        if page >= (data.get("total_pages") or data.get("totalPages") or 1):
            break
    return items


# ---------- lists (duplicate check, matching in the review) ----------

ENDPOINTS = {"food": "/foods", "unit": "/units", "keyword": "/organizers/tags", "category": "/organizers/categories"}


def fetch_all_items(client, endpoint: str, max_pages: int = MAX_PAGES) -> list[dict]:
    """[{"id", "name", "plural_name", "numchild"}] - the shape the matching
    in the review expects (Mealie tags have no groups: numchild 0)."""
    return [
        {"id": item.get("id"), "name": item.get("name"),
         "plural_name": item.get("pluralName") or item.get("plural_name"), "numchild": 0}
        for item in _paged(client, ENDPOINTS[endpoint]) if item.get("name")
    ]


def fetch_all_foods_full(client) -> list[dict]:
    return fetch_all_items(client, "food")


def fetch_all_recipe_names(client, max_pages: int = MAX_PAGES) -> list[str]:
    return [item["name"] for item in _paged(client, "/recipes") if item.get("name")]


def fetch_all_keyword_names(client, max_pages: int = MAX_PAGES) -> list[str]:
    return [item["name"] for item in fetch_all_items(client, "keyword")]


# ---------- creating ----------

class _Lookup:
    """Name -> existing entry for foods / units / tags / categories, loaded
    once per import and extended with what gets created."""

    def __init__(self, client):
        self.client = client
        self.cache: dict[str, dict[str, dict]] = {}

    def get_or_create(self, endpoint: str, name: str) -> dict:
        name = name.strip()[:NAME_MAX_LENGTH]
        if not name:
            raise TandoorError("Cannot create an entry with an empty name.")
        if endpoint not in self.cache:
            self.cache[endpoint] = {}
            for item in _paged(self.client, ENDPOINTS[endpoint]):
                for key in (item.get("name"), item.get("pluralName")):
                    if key:
                        self.cache[endpoint].setdefault(key.strip().casefold(), item)
        found = self.cache[endpoint].get(name.casefold())
        if found:
            return found
        created = _check(self.client.post(ENDPOINTS[endpoint], json={"name": name}), f"creating {name!r}").json()
        self.cache[endpoint][name.casefold()] = created
        return created


def _ref(item: dict, with_slug: bool = False) -> dict:
    ref = {"id": item["id"], "name": item["name"]}
    if with_slug and item.get("slug"):
        ref["slug"] = item["slug"]
    return ref


def _minutes(value) -> str | None:
    return f"{int(value)} min" if value else None


def build_payload(recipe: ExtractedRecipe, lookup: _Lookup) -> dict:
    ingredients, refs_by_step = [], {}
    n_steps = max(len(recipe.steps), 1)
    for ing in recipe.ingredients:
        reference_id = str(uuid.uuid4())
        entry = {
            "referenceId": reference_id,
            "quantity": ing.amount if ing.amount is not None else 0,
            "unit": _ref(lookup.get_or_create("unit", ing.unit)) if ing.unit else None,
            "food": _ref(lookup.get_or_create("food", ing.name)) if ing.name else None,
            "note": ing.note or "",
            "title": ing.group or None,
        }
        ingredients.append(entry)
        step = ing.step_index if ing.step_index is not None and 0 <= ing.step_index < n_steps else 0
        refs_by_step.setdefault(step, []).append({"referenceId": reference_id})

    instructions = [
        {"id": str(uuid.uuid4()), "title": step.title or "", "summary": "", "text": step.instruction,
         "ingredientReferences": refs_by_step.get(i, [])}
        for i, step in enumerate(recipe.steps)
    ]
    tags = []
    for tag in recipe.tags:
        try:
            tags.append(_ref(lookup.get_or_create("keyword", tag), with_slug=True))
        except TandoorError as exc:  # a single bad tag shouldn't block the import
            log.info("Mealie tag %r skipped: %s", tag, exc)

    payload = {
        "description": (recipe.description or "")[:1000],
        "recipeIngredient": ingredients,
        "recipeInstructions": instructions,
        "tags": tags,
    }
    if recipe.servings:
        payload["recipeServings"] = recipe.servings
        payload["recipeYield"] = str(recipe.servings)
    for field, minutes in (("prepTime", recipe.prep_time_minutes), ("performTime", recipe.cook_time_minutes),
                           ("totalTime", recipe.total_time_minutes)):
        if _minutes(minutes):
            payload[field] = _minutes(minutes)
    if recipe.source_url:
        payload["orgURL"] = recipe.source_url
    return payload


_lookups: dict[int, _Lookup] = {}


def _lookup_for(client) -> _Lookup:
    # one lookup per client, i.e. per import run
    lookup = _lookups.get(id(client))
    if lookup is None or lookup.client is not client:
        _lookups.clear()
        lookup = _lookups[id(client)] = _Lookup(client)
    return lookup


def create_recipe(client, recipe: ExtractedRecipe) -> str:
    """Returns the new recipe's slug (Mealie's id in URLs)."""
    lookup = _lookup_for(client)
    payload = build_payload(recipe, lookup)
    try:
        slug = _check(client.post("/recipes", json={"name": recipe.title[:NAME_MAX_LENGTH]}), "the recipe").json()
    except httpx.HTTPError as exc:
        raise TandoorError(f"Network error creating the recipe: {exc}") from exc
    try:
        _check(client.patch(f"/recipes/{slug}", json=payload), "the recipe's details")
    except Exception:
        delete_recipe(client, slug)  # no empty shell left behind
        raise
    return slug


def delete_recipe(client, recipe_id) -> None:
    try:
        resp = client.delete(f"/recipes/{recipe_id}")
    except httpx.HTTPError as exc:
        raise TandoorError(f"Network error deleting the recipe: {exc}") from exc
    if resp.status_code != 404:
        _check(resp, "deleting the recipe")


def upload_image(client, recipe_id, image_path: str) -> None:
    if not os.path.exists(image_path):
        return
    extension = os.path.splitext(image_path)[1].lstrip(".").lower() or "jpg"
    with open(image_path, "rb") as f:
        resp = client.put(f"/recipes/{recipe_id}/image",
                          files={"image": (os.path.basename(image_path), f, "application/octet-stream")},
                          data={"extension": extension})
    _check(resp, "the image")


def list_cookbooks(client) -> list[str]:
    """The cookbooks of an import are categories - their names."""
    return sorted((i["name"] for i in fetch_all_items(client, "category")), key=str.casefold)


def get_or_create_cookbook(client, name: str) -> tuple[str, str]:
    """The cookbook becomes a category - (category id, "category")."""
    return _lookup_for(client).get_or_create("category", name)["id"], "category"


def add_recipe_to_cookbook(client, cookbook_id, cookbook_endpoint, recipe_id) -> None:
    category = next((c for c in _lookup_for(client).cache.get("category", {}).values() if c["id"] == cookbook_id), None)
    if category is None:
        return
    current = _check(client.get(f"/recipes/{recipe_id}"), "loading the recipe").json()
    categories = [c for c in current.get("recipeCategory") or [] if c.get("id") != cookbook_id]
    categories.append(_ref(category, with_slug=True))
    _check(client.patch(f"/recipes/{recipe_id}", json={"recipeCategory": categories}), "the category")


# ---------- connection ----------

def test_connection() -> dict:
    with get_client() as client:
        return _check(client.get("/users/self"), "the API token").json()


def recipe_url_base() -> str | None:
    """Base for links to imported recipes: <url>/g/<group>/r/<slug>."""
    if not settings.mealie_url:
        return None
    try:
        group = test_connection().get("groupSlug") or "home"
    except Exception:  # noqa: BLE001
        group = "home"
    return f"{settings.mealie_url.rstrip('/')}/g/{group}/r/"
