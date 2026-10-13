"""Amounts in the steps, written while the recipe is read: the extraction
puts a marker where a step mentions one of its ingredients - "[[0]]" for
the recipe's first ingredient - and never a number. Kept in the recipe as
"[[i0:Mehl]]": the ingredient's ref (stable while the review reorders or
reassigns ingredients) and its name (what's left when it gets deleted).

On import the markers become
- Tandoor: its template "{{ ingredients[n] }}" (n = the place in the step;
  " ({{ ingredients[n].note }})" for a comment) - the amount follows when
  the servings change; an ingredient without an amount, or one that moved
  to another step, reads as its plain name;
- Mealie: the plain name (no templates - a written-out amount would be
  wrong as soon as the servings change).
The review's preview shows them with the amount.

This replaces a second AI call per recipe after the import (recipe_amounts,
which still handles recipes from elsewhere and older ones)."""
from __future__ import annotations

import re

RAW = re.compile(r"\[\[\s*(\d+)\s*\]\]")          # as the AI writes it
MARK = re.compile(r"\[\[(i\d+):([^\]\[]*)\]\]")    # as the recipe keeps it
NUMBER = r"\d+(?:[.,]\d+)?(?:\s*[-–/]\s*\d+(?:[.,]\d+)?)?"
# An article right before a marker reads double ("die 250 g Mehl") - dropped.
ARTICLE = re.compile(r"\b(?:der|die|das|den|dem|des|ein|eine|einen|einem|einer|the|an?|le|la|les|l'|il|lo|gli|el|los|las)\s+(?=\[\[)",
                     re.I)

PROMPT = """IMPORTANT - Amounts in the steps: in each step's instruction, replace the \
FIRST mention of each ingredient whose step_index is that step by the marker [[n]] \
- n is the ingredient's zero-based position in this recipe's "ingredients" array. \
The marker is later shown as the ingredient WITH its amount, unit and note (e.g. \
"250 g flour (sifted)"), so drop the article and any amount or unit before it and \
words its note already says: "Mix the flour with the milk" -> "Mix [[0]] with \
[[1]]". Word the sentence so the amount fits ("Add [[2]] and stir", not "stir with \
[[2]]" where the grammar would need another form). Never write an amount or unit \
yourself; no marker for an ingredient without an amount (e.g. salt to taste); each \
marker at most once per recipe; only the step's own ingredients. Everything else in \
the instruction stays as it would be without markers.
"""


def wanted() -> bool:
    """Only for Tandoor - Mealie has no templates (see the module text)."""
    from .config import settings
    return (settings.recipe_manager or "tandoor").strip().lower() != "mealie"


def from_extraction(item: dict) -> dict:
    """The AI's recipe (dict) with refs on its ingredients and "[[n]]" turned
    into "[[iN:name]]" - unknown, repeated or amount-less ones into the name."""
    ingredients = item.get("ingredients") or []
    for n, ing in enumerate(ingredients):
        if isinstance(ing, dict):
            ing["ref"] = f"i{n}"
    used = set()

    def mark(match):
        n = int(match.group(1))
        ing = ingredients[n] if n < len(ingredients) and isinstance(ingredients[n], dict) else None
        if ing is None:
            return ""
        name = str(ing.get("name") or "").replace("]", "").replace("[", "")
        if ing.get("amount") in (None, 0) or n in used:
            return name
        used.add(n)
        return f"[[i{n}:{name}]]"

    for step in item.get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("instruction"), str) and "[[" in step["instruction"]:
            text = RAW.sub(mark, step["instruction"])
            text = ARTICLE.sub("", text)
            text = DOUBLED.sub(lambda m: _undouble(m, ingredients), text)  # "250 g [[i0:Mehl]]"
            step["instruction"] = re.sub(r"(?<=\S)  +(?=\S)", " ", text).strip()
    return item


# an amount (and unit) the AI still wrote right before a marker
DOUBLED = re.compile(rf"(?P<num>{NUMBER})\s*(?P<unit>[^\W\d]{{1,6}}\.?)?\s+(?P<mark>\[\[i(?P<n>\d+):[^\]\[]*\]\])")


def _undouble(match, ingredients) -> str:
    """Drops the amount before a marker - only when it is that ingredient's
    own amount (not "after 3 min. [[i0:Salz]]")."""
    try:
        written = float(match.group("num").replace(",", ".").split("-")[0].split("–")[0].split("/")[0])
        amount = float(ingredients[int(match.group("n"))].get("amount") or 0)
    except (ValueError, IndexError, AttributeError):
        return match.group(0)
    return match.group("mark") if abs(written - amount) < 1e-9 else match.group(0)


def plain(text: str) -> str:
    """The text with every marker as its ingredient's name."""
    return MARK.sub(lambda m: m.group(2), text or "")


def has_markers(text: str) -> bool:
    return bool(MARK.search(text or ""))


def to_tandoor(text: str, step_ingredients) -> str:
    """step_ingredients: this step's Ingredient objects, in their order in the step."""
    position = {ing.ref: n for n, ing in enumerate(step_ingredients) if getattr(ing, "ref", None)}

    def put(match):
        ref, name = match.group(1), match.group(2)
        n = position.get(ref)
        if n is None:
            return name  # deleted in the review, or moved to another step
        ing = step_ingredients[n]
        if not ing.amount:
            return ing.name  # nothing to scale - and the template would read "0 Salz"
        return f"{{{{ ingredients[{n}] }}}}" + (f" ({{{{ ingredients[{n}].note }}}})" if (ing.note or "").strip() else "")
    return MARK.sub(put, text or "")
