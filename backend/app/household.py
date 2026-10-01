"""The household profile (Plan -> Household): how many people eat, what
must never be in a dish (allergies, intolerances), what they'd rather not
eat, and fixed wishes per weekday ("Friday: pizza"). The weekly plan, its
chat and "What can I cook today?" follow it:

- recipes with an "avoid" ingredient are left out before the AI sees them
  (matched against the recipe index like the at-home ingredients) - the
  AI is told as well, for ingredients the index doesn't know;
- dislikes and the weekday wishes go to the AI; "What can I cook today?"
  ranks disliked recipes last;
- planned days get the number of persons as servings (Tandoor's shopping
  list scales the amounts; with Mealie the recipe is added that many times
  over, relative to its servings)."""
from __future__ import annotations

import calendar
import re

from . import app_settings, cook_today


def get() -> dict:
    return app_settings.get()["household"]


def _words(text) -> list[str]:
    return [cook_today._norm(w) for w in re.split(r"[,;\n]+", text or "") if cook_today._norm(w)]


def _hits(words, foods, title="") -> list[str]:
    """Which of `words` are in the recipe - an ingredient ([name, plural]
    pairs, forgiving match as for the at-home list) or the title."""
    title = cook_today._norm(title)
    return [w for w in words
            if any(cook_today._matches(w, names) for names in foods)
            or (len(w) >= cook_today.MIN_PART_LEN and w in title)]


def avoided_in(foods, title="", profile=None) -> list[str]:
    return _hits(_words((profile or get())["avoid"]), foods, title)


def disliked_in(foods, title="", profile=None) -> list[str]:
    return _hits(_words((profile or get())["dislikes"]), foods, title)


def is_empty(profile=None) -> bool:
    p = profile or get()
    return not (p["persons"] or p["avoid"] or p["dislikes"] or p["weekdays"] or p.get("max_kcal")
                or p.get("min_protein") or p.get("goals"))


def too_rich(values, profile=None) -> bool:
    """Known energy per serving above the household's limit."""
    limit = (profile or get()).get("max_kcal")
    return bool(limit and values.get("kcal") and values["kcal"] > limit)


def for_ai(profile=None) -> dict:
    """The profile as the planning prompts read it."""
    p = profile or get()
    return {
        "persons": p["persons"] or None,
        "never": _words(p["avoid"]),
        "dislikes": _words(p["dislikes"]),
        "fixed_days": {calendar.day_name[int(k)]: v for k, v in sorted(p["weekdays"].items())},
        "max_kcal_per_serving": p.get("max_kcal") or None,
        "min_protein_per_serving": p.get("min_protein") or None,
        "goals": p.get("goals") or "",
    }


def servings(recipe_servings, profile=None) -> int | None:
    """The servings to plan with: the household's persons, if set."""
    return (profile or get())["persons"] or recipe_servings or None
