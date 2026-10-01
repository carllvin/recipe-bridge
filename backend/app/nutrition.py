"""Energy and protein per serving of a recipe - from what the recipe
manager knows: Tandoor computes the recipe's nutrition from its
ingredients' properties ("food_properties", the whole recipe - divided by
the servings here), Mealie keeps it per serving ("nutrition", free text
like "450 kcal"). None where it isn't known."""
from __future__ import annotations

import re

from .nutrition_properties import nutrient_of


def _number(value) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    return float(m.group(0).replace(",", ".")) if m else None


def per_serving(recipe) -> dict:
    """{"kcal": float|None, "protein": float|None}"""
    found = {"kcal": None, "protein": None}
    mealie = recipe.get("nutrition")
    if isinstance(mealie, dict) and mealie:
        found["kcal"] = _number(mealie.get("calories"))
        found["protein"] = _number(mealie.get("proteinContent"))
        return found
    props = recipe.get("food_properties")
    servings = recipe.get("servings") or 0
    if isinstance(props, dict) and servings > 0:
        for prop in props.values():
            if not isinstance(prop, dict):
                continue
            key = {"energy_kcal": "kcal", "protein_g": "protein"}.get(nutrient_of(prop.get("name")))
            total = prop.get("total_value")
            if key and isinstance(total, (int, float)) and total > 0:
                found[key] = round(total / servings, 1)
    return found


def line(values) -> str:
    """For the AI: "520 kcal, 32 g protein" or "-"."""
    parts = []
    if values.get("kcal"):
        parts.append(f"{values['kcal']:.0f} kcal")
    if values.get("protein"):
        parts.append(f"{values['protein']:.0f} g protein")
    return ", ".join(parts) or "-"
