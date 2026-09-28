""""What can I cook today?" - finds recipes in the collection that fit the
ingredients you have at home. No AI: a small index of every recipe's
ingredients (built while the health overview is counted, which reads every
recipe anyway) is matched against what you type in.

Matching is forgiving: "Tomate" matches "Tomaten" and "Cherrytomaten",
"Feta" matches "Feta-Käse". Pantry staples (salt, pepper, oil, onions,
garlic ...) are assumed to be at home and don't count either way."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

from . import mealie_plan, seasonal, tandoor_client, target
from .config import settings
from .tandoor_helpers import fetch_all_recipes_full

log = logging.getLogger("tandoor-helper")

INDEX_MAX_AGE_HOURS = 24
MIN_PART_LEN = 4  # shorter words only match whole names ("Ei" must not match "Eis")

# Always assumed to be at home. STAPLES also match as the end of a compound
# word ("Olivenöl", "Meersalz", "Puderzucker"); STAPLE_WORDS only as the
# last word of the name ("Rote Zwiebel", "Knoblauchzehen" - but not
# "Frühlingszwiebel", which is a different vegetable).
STAPLES = ["salz", "pfeffer", "wasser", "öl", "zucker", "mehl", "butter", "essig",
           "salt", "pepper", "water", "oil", "sugar", "flour", "vinegar"]
STAPLE_WORDS = {"zwiebel", "zwiebeln", "knoblauch", "knoblauchzehe", "knoblauchzehen",
                "onion", "onions", "garlic"}

_build = {"running": False}
_build_lock = threading.Lock()


def _path() -> str:
    return os.path.join(settings.data_dir, "recipe_index.json")


def _norm(text) -> str:
    return re.sub(r"[^\w]+", " ", (text or "").casefold()).strip()


def build_index(recipes) -> list[dict]:
    index = []
    for r in recipes:
        foods = {}
        for step in r.get("steps", []):
            for ing in step.get("ingredients", []):
                food = ing.get("food") or {}
                if food.get("id") is None or ing.get("is_header"):
                    continue
                foods[food["id"]] = [food.get("name", ""), food.get("plural_name") or ""]
        index.append({
            "id": r["id"], "slug": r.get("slug"), "name": r.get("name", ""), "foods": list(foods.values()),
            "minutes": (r.get("working_time") or 0) + (r.get("waiting_time") or 0),
            "rating": r.get("rating"), "last_cooked": r.get("last_cooked"),
        })
    return index


def save_index(recipes) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"built_at": time.time(), "recipes": build_index(recipes)}, f, ensure_ascii=False)
    os.replace(tmp, _path())


def seasonal_by_recipe() -> dict[int, list[str]]:
    """Recipe id -> its in-season ingredients (display names), from the
    index; empty if there is no index yet."""
    data = _load()
    if not data:
        return {}
    result = {}
    for r in data["recipes"]:
        keys = seasonal.seasonal_in(n for names in r["foods"] for n in names)
        if keys:
            result[r["id"]] = seasonal.display_names(keys)
    return result


def _load() -> dict | None:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _rebuild() -> None:
    try:
        if target.is_mealie():
            from .mealie_maintenance import fetch_recipes_full
            with target.client().get_client() as client:
                save_index([mealie_plan.as_tandoor(r) for r in fetch_recipes_full(client)])
            return
        with tandoor_client.get_client() as client:
            save_index(fetch_all_recipes_full(client))
    except Exception:  # noqa: BLE001
        log.exception("Building the recipe index failed")
    finally:
        _build["running"] = False


def ensure_fresh() -> bool:
    """Starts a background rebuild if the index is missing or older than a
    day. Returns True while a build is running."""
    data = _load()
    stale = data is None or data.get("built_at", 0) < time.time() - INDEX_MAX_AGE_HOURS * 3600
    with _build_lock:
        if stale and not _build["running"]:
            _build["running"] = True
            threading.Thread(target=_rebuild, daemon=True).start()
    return _build["running"]


def _parts(text) -> list[str]:
    return [p for p in _norm(text).split() if p]


def _matches(have: str, food_names) -> bool:
    """`have` (normalized) is the food, its plural, or a long enough part of
    it - either way round ("tomate" ~ "cherrytomaten", "feta käse" ~ "feta")."""
    for name in food_names:
        name = _norm(name)
        if not name:
            continue
        if have == name:
            return True
        if len(have) >= MIN_PART_LEN and have in name:
            return True
        if len(name) >= MIN_PART_LEN and name in have:
            return True
        # singular typed, plural stored or vice versa ("zwiebel" / "zwiebeln")
        if len(have) >= MIN_PART_LEN and (name.startswith(have) or have.startswith(name)) and abs(len(name) - len(have)) <= 2:
            return True
    return False


def _is_staple(food_names) -> bool:
    """See STAPLES / STAPLE_WORDS."""
    for name in food_names:
        name = _norm(name)
        if not name:
            continue
        if any(name == s or name.endswith(s) or name.startswith(s + " ") for s in STAPLES):
            return True
        if name.split()[-1] in STAPLE_WORDS:
            return True
    return False


def suggest(have_text: str, limit: int = 20) -> dict:
    data = _load()
    building = ensure_fresh()
    if data is None:
        return {"building": True, "results": []}
    have = [_norm(h) for h in re.split(r"[,;\n]+", have_text or "") if _norm(h)]
    if not have:
        return {"building": building, "results": [], "built_at": data.get("built_at")}
    results = []
    for r in data["recipes"]:
        needed, matched, missing = 0, [], []
        for names in r["foods"]:
            if _is_staple(names):
                continue  # a staple - not counted either way
            needed += 1
            if any(_matches(h, names) for h in have):
                matched.append(names[0])
            else:
                missing.append(names[0])
        if not matched:
            continue
        season = seasonal.display_names(seasonal.seasonal_in(n for names in r["foods"] for n in names))
        results.append({"id": r["id"], "slug": r.get("slug"), "name": r["name"], "minutes": r["minutes"] or None, "rating": r["rating"],
                        "matched": matched, "missing": missing, "needed": needed, "season": season})
    # Fewest missing first, then most of what you have used, then seasonal
    # ingredients, then rating.
    results.sort(key=lambda x: (len(x["missing"]), -len(x["matched"]), -len(x["season"]), -(x["rating"] or 0)))
    return {"building": building, "results": results[:limit], "built_at": data.get("built_at")}


# ---------- ingredients from a photo of the fridge / pantry ----------

PHOTO_MAX_SIDE = 1600
PHOTO_PROMPT = """This is a photo of a fridge, pantry shelf or kitchen counter.
List the food items a cook could use that are clearly visible - fresh
produce, dairy, meat, fish, eggs, bread, and packaged staples whose content
you can read or recognize. Use plain base names in {language}, singular where
natural, without brand names or amounts (e.g. "Zucchini", "Feta", "Eier",
"Kichererbsen"). Leave out anything you can't identify with confidence,
drinks, and items like salt, pepper and oil.

Respond with ONLY a JSON array of strings (no explanation, no markdown fence)."""


def ingredients_from_photos(photos: list[bytes]) -> tuple[list[str], int, int]:
    """(ingredient names, input tokens, output tokens) - one AI call per
    photo with the main model (it reads images); names merged without
    duplicates."""
    import io

    from PIL import Image, ImageOps

    from . import json_answer, llm_provider

    prompt = PHOTO_PROMPT.replace("{language}", settings.output_language or "English")
    names, seen, tokens_in, tokens_out = [], set(), 0, 0
    for data in photos:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        img.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        text, usage = llm_provider.transcribe_image(buf.getvalue(), prompt, max_tokens=800)
        tokens_in += usage.input_tokens
        tokens_out += usage.output_tokens
        try:
            found = json_answer.parse(text)
        except ValueError:
            log.warning("Fridge photo: unreadable answer %r", text[:200])
            continue
        for name in found if isinstance(found, list) else []:
            name = str(name).strip()
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                names.append(name)
    return names, tokens_in, tokens_out
