"""Which recipe manager the import goes to: Tandoor (default) or Mealie
(RECIPE_MANAGER=mealie). Only the import path asks this module for its
client; the maintenance and planning tools always talk to Tandoor and are
hidden with Mealie."""
from __future__ import annotations

from . import mealie_client, tandoor_client, tools_ingredients
from .config import settings


def is_mealie() -> bool:
    return (settings.recipe_manager or "tandoor").strip().lower() == "mealie"


def name() -> str:
    return "Mealie" if is_mealie() else "Tandoor"


def client():
    """The module with get_client(), create_recipe(), upload_image(), ..."""
    return mealie_client if is_mealie() else tandoor_client


def fetch_foods(c) -> list[dict]:
    return mealie_client.fetch_all_foods_full(c) if is_mealie() else tools_ingredients.fetch_all_foods_full(c)


def base_url() -> str | None:
    url = settings.mealie_url if is_mealie() else settings.tandoor_url
    return url.rstrip("/") if url else None


def recipe_url_base() -> str | None:
    """Link to an imported recipe = this + its id (Tandoor) or slug (Mealie)."""
    if is_mealie():
        return mealie_client.recipe_url_base()
    return f"{settings.tandoor_url.rstrip('/')}/view/recipe/" if settings.tandoor_url else None
