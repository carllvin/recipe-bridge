from __future__ import annotations

import json
import logging
import uuid

from . import duplicates, ignored, llm_provider, nutrition_properties, tandoor_client, tool_jobs
from .config import get_language_code, settings
from .schemas import ToolSuggestion
from .tandoor_helpers import chunked, delete_entity, entity_exists, find_recipes_by_filter, format_cost_estimate, minimal_ref, resolve_name_collisions, validate_actions

log = logging.getLogger("recipe-bridge")

CHUNK_SIZE = 80

REVIEW_SYSTEM_PROMPT = """You are cleaning up a home cook's ingredient
database. Its target language is {language}. You will receive a JSON array
of existing ingredient entries, each {{"id": integer, "name": string}}.

Find three kinds of problems:
1. A name that is a prepared/adjective form ("geriebener Parmesan", "gehackte
   Zwiebeln") rather than a plain base ingredient noun ("Parmesan",
   "Zwiebel") - propose a corrected, singular, plain name for it.
2. A name that is NOT already in {language} - propose a natural {language}
   translation of it (still following rule 1: plain singular noun, not a
   prepared form).
3. Two or more entries that clearly refer to the exact same base ingredient
   once you account for both of the above (singular/plural spelling
   variants, near-duplicate spelling, a prepared-form or other-language
   duplicate of an already-clean {language} entry) - these should be merged
   into one. Prefer keeping the entry that is already a clean,
   correctly-spelled, singular, plain {language} noun; if more than one
   qualifies, prefer the shorter one. If none of the group is already clean
   {language}, invent the keep_name yourself (translated + cleaned) even
   though no existing entry currently has it.

Every id may appear in AT MOST ONE element of your answer - put all ids
of one ingredient into a single merge instead of listing a rename and a merge
for the same entry.

Only include entries that actually need a change - do not list names that
are already a clean, plain, singular {language} noun. Respond with ONLY a
JSON array (no explanation, no markdown fence), each element one of:

{{"type": "rename", "id": <id>, "new_name": <corrected name>}}
{{"type": "merge", "keep_id": <id to keep>, "keep_name": <possibly corrected/translated name for it>, "remove_ids": [<other ids that mean the same thing>]}}

If nothing needs a change, respond with [].
"""


def _review_chunk(foods, language):
    system_prompt = REVIEW_SYSTEM_PROMPT.replace("{language}", language)
    text_out, usage = llm_provider.complete_tool_text(
        system_prompt,
        json.dumps([{"id": f["id"], "name": f["name"]} for f in foods], ensure_ascii=False),
        max_tokens=8000,
    )
    text_out = text_out.strip().strip("`")
    if text_out.startswith("json"):
        text_out = text_out[4:]
    return json.loads(text_out), usage


def _describe(action, by_id):
    if action["type"] == "rename":
        old = by_id.get(action["id"], {}).get("name", "?")
        return f"rename {old!r} -> {action['new_name']!r}"
    keep_old = by_id.get(action["keep_id"], {}).get("name", "?")
    remove_names = ", ".join(f"{by_id.get(rid, {}).get('name', '?')!r}" for rid in action["remove_ids"])
    return f"merge {remove_names} into {keep_old!r} -> {action['keep_name']!r}"


def run_scan(job_id: str) -> None:
    """Runs in a background thread (see main.py). Scans all foods, reviews
    them in chunks via the AI, and populates the job with one ToolSuggestion
    per proposed change - same logic as manage_ingredients.py's `review`
    subcommand, adapted to populate a job for the UI to poll instead of
    printing to a terminal and asking for confirmation there."""
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        return

    try:
        if not llm_provider.is_configured():
            job.status = "error"
            job.error = llm_provider.missing_key_hint()
            tool_jobs.save_tool_job(job)
            return

        with tandoor_client.get_client() as client:
            foods = tandoor_client.fetch_all_items(client, "food")
            all_foods = foods
            if job.meta.get("focus") == "duplicates":
                # Only the likely duplicates from the health overview, each
                # group kept together in one chunk.
                by_id = {f["id"]: f for f in foods}
                groups = duplicates.open_duplicate_ids(
                    duplicates.food_duplicates(fetch_all_foods_full(client)), "foods_duplicates")
                chunks = [[by_id[i] for i in chunk if i in by_id]
                          for chunk in duplicates.pack_groups(groups, CHUNK_SIZE)]
                foods = [f for chunk in chunks for f in chunk]
            else:
                chunks = list(chunked(foods, CHUNK_SIZE))
            job.progress_total = len(foods)
            job.cost_estimate = format_cost_estimate(len(foods), "chunked_review")
            tool_jobs.save_tool_job(job)

            all_actions = []
            for i, chunk in enumerate(chunks, 1):
                if job.cancel_requested:
                    break
                job.progress_label = f"Reviewing chunk {i}/{len(chunks)}..."
                tool_jobs.save_tool_job(job)
                try:
                    actions, usage = _review_chunk(chunk, settings.output_language)
                    all_actions.extend(validate_actions(actions, "ingredients_review"))
                    job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
                    job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
                except Exception as exc:  # noqa: BLE001
                    log.warning("Ingredients review chunk %d failed: %s", i, exc)
                job.progress_current = min(sum(len(c) for c in chunks[:i]), len(foods))
                tool_jobs.save_tool_job(job)

            # Whether the loop finished naturally or was cancelled partway
            # through, build suggestions from whatever was gathered - a
            # cancelled scan shouldn't throw away chunks that already cost
            # real AI calls and produced valid suggestions.
            all_actions = duplicates.drop_ignored_merges(resolve_name_collisions(all_actions, all_foods), "foods_duplicates")
            by_id = {f["id"]: f for f in all_foods}

            job.suggestions = [
                ToolSuggestion(id=uuid.uuid4().hex[:10], kind=action["type"], summary=_describe(action, by_id), detail=action)
                for action in all_actions
            ]
            job.status = "cancelled" if job.cancel_requested else "ready"
            job.progress_label = None
            tool_jobs.save_tool_job(job)

    except Exception as exc:  # noqa: BLE001
        log.exception("Ingredients review scan failed for job %s", job_id)
        job.status = "error"
        job.error = str(exc)
        tool_jobs.save_tool_job(job)


def _find_recipes_using_food(client, food_id):
    return find_recipes_by_filter(client, "foods", food_id)


def _repoint_recipe_food(recipe_detail, remove_id, keep_id, keep_name):
    new_steps = []
    for step in recipe_detail.get("steps", []):
        new_ingredients = []
        for ing in step.get("ingredients", []):
            new_ing = dict(ing)
            food_ref = ing.get("food")
            if food_ref and food_ref.get("id") == remove_id:
                new_ing["food"] = {"id": keep_id, "name": keep_name}
            elif food_ref is not None:
                new_ing["food"] = minimal_ref(food_ref)
            if ing.get("unit") is not None:
                new_ing["unit"] = minimal_ref(ing["unit"])
            new_ingredients.append(new_ing)
        new_step = dict(step)
        new_step["ingredients"] = new_ingredients
        new_steps.append(new_step)
    return {"steps": new_steps}


def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    """Applies exactly one suggestion (called by the web UI's per-item Apply
    button) and updates its status in place. The suggestion is left with
    status="error" and the message on failure; the job's other suggestions
    are unaffected."""
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise tandoor_client.TandoorError("Job not found.")
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise tandoor_client.TandoorError("Suggestion not found.")
    if suggestion.status != "pending":
        return suggestion

    action = suggestion.detail
    try:
        with tandoor_client.get_client() as client:
            if action["type"] == "rename":
                resp = client.patch(f"/food/{action['id']}/", json={"name": action["new_name"]})
                if resp.status_code not in (200, 201):
                    raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
            else:  # merge
                keep_id, keep_name = action["keep_id"], action["keep_name"]
                if not entity_exists(client, "food", keep_id):
                    raise tandoor_client.TandoorError(
                        f"Food #{keep_id} no longer exists (already merged by another suggestion?) - rescan to continue."
                    )
                resp = client.patch(f"/food/{keep_id}/", json={"name": keep_name})
                if resp.status_code not in (200, 201):
                    raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")

                for remove_id in action["remove_ids"]:
                    if not entity_exists(client, "food", remove_id):
                        continue  # already merged away by an earlier suggestion
                    for recipe in _find_recipes_using_food(client, remove_id):
                        resp = client.get(f"/recipe/{recipe['id']}/")
                        resp.raise_for_status()
                        payload = _repoint_recipe_food(resp.json(), remove_id, keep_id, keep_name)
                        resp = client.patch(f"/recipe/{recipe['id']}/", json=payload)
                        if resp.status_code not in (200, 201):
                            raise tandoor_client.TandoorError(
                                f"Could not update recipe {recipe['id']}: {resp.status_code} {resp.text[:300]}"
                            )

                    still_used = _find_recipes_using_food(client, remove_id)
                    if still_used:
                        raise tandoor_client.TandoorError(
                            f"Still used by {len(still_used)} recipe(s) after repointing - not deleting #{remove_id}."
                        )
                    delete_entity(client, "food", remove_id)

        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status = "error"
        suggestion.error = str(exc)
    finally:
        tool_jobs.save_tool_job(job)

    return suggestion


# ---------- Enrich: fill in missing plural + nutrition ----------

ENRICH_CHUNK_SIZE = 40
SUPERMARKET_CATEGORY_ENDPOINT = "supermarket-category"
NUTRIENT_KEYS = ["energy_kcal", "protein_g", "fat_g", "carbs_g"]  # keys in the AI's answer

ENRICH_SYSTEM_PROMPT = """You fill in missing data for ingredient entries in
a home cook's database. The language is {language}. You will receive a JSON
object {"categories": [{"id": integer, "name": string}, ...], "ingredients":
[...]}, each ingredient {"id": integer, "name": string, "needs_plural": bool,
"needs_nutrition": bool, "needs_category": bool}.

For every ingredient, answer:
- "plural_name" (only if needs_plural): the plural of the name in
  {language} - but ONLY for things a recipe counts in pieces ("2 Tomaten",
  "3 Eier", "2 rote Zwiebeln", "4 Knoblauchzehen", "2 Hähnchenbrustfilets"),
  e.g. "Tomate" -> "Tomaten", "Ei" -> "Eier", "Rote Zwiebel" -> "Rote
  Zwiebeln". Use null for everything measured by weight, volume or spoons
  instead of counted: pastes, sauces, oils, vinegars, spices, dried herbs,
  flour, sugar, salt, liquids, dairy, grains, minced meat, jams, powders
  (e.g. "Koreanische Chilipaste", "Sojasauce", "Olivenöl", "Mehl",
  "Zimt", "Milch", "Reis", "Hackfleisch"). Also null if the plural is
  spelled exactly like the singular (e.g. "Zucker", "Messer"). When in
  doubt, use null - a missing plural is harmless, a nonsensical one isn't.
- "nutrition" (only if needs_nutrition): rough typical values for the raw /
  commonly used ingredient, as {"basis": "g" or "ml", "energy_kcal": number,
  "protein_g": number, "fat_g": number, "carbs_g": number} per 100 g - or
  per 100 ml for liquids (then basis "ml"). Use well-known reference values
  (USDA-style), rounded sensibly. Use null if the name is too vague to
  estimate (e.g. "Gewürzmischung nach Wahl").
- "category_id" (only if needs_category): the id of the supermarket
  category from "categories" where you'd find this ingredient when shopping.
  ONLY use an id from that list - never invent a category. Use null if none
  of them genuinely fits.

Respond with ONLY a JSON array (no explanation, no markdown fence), one
element per ingredient:
{"id": <id>, "plural_name": <string or null>, "nutrition": <object or null>, "category_id": <integer or null>}
"""


def fetch_all_foods_full(client):
    """Paginated /food/ list. Tandoor's list view already contains
    plural_name and properties, so no per-food GET is needed."""
    foods = []
    url, params = "/food/", {"page_size": 200}
    for _ in range(100):
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        foods.extend(data.get("results", data) if isinstance(data, dict) else data)
        url = data.get("next") if isinstance(data, dict) else None
        if not url:
            break
        params = None
    return foods


def fetch_supermarket_categories(client):
    """Existing supermarket categories as [{"id", "name"}]. Empty list if the
    endpoint isn't available - the category part is then simply skipped."""
    try:
        return tandoor_client.fetch_all_items(client, SUPERMARKET_CATEGORY_ENDPOINT)
    except tandoor_client.TandoorError as exc:
        log.warning("Could not load supermarket categories, skipping them: %s", exc)
        return []


def _same_word(a, b):
    return (a or "").strip().lower() == (b or "").strip().lower()


# German word endings of ingredients that are measured, never counted - a
# safety net for plurals the AI still suggests ("Chilipaste" -> "Chilipasten").
# Checked against the last word of the name, so "Koreanische Chilipaste" and
# "Sesamöl" match but "Wassermelone" doesn't.
_GERMAN_MASS_ENDINGS = (
    "paste", "pasta", "soße", "sosse", "sauce", "öl", "essig", "mehl", "grieß", "gries",
    "stärke", "pulver", "salz", "zucker", "sirup", "honig", "dicksaft", "milch", "sahne",
    "rahm", "schmand", "joghurt", "jogurt", "quark", "butter", "schmalz", "margarine",
    "creme", "crème", "brühe", "fond", "saft", "wein", "bier", "likör", "wasser",
    "senf", "ketchup", "mayonnaise", "pesto", "dressing", "marmelade", "konfitüre",
    "gelee", "mus", "püree", "mark", "extrakt", "aroma", "hefe", "gelatine", "natron",
    "reis", "hack", "hackfleisch", "flocken", "gewürz", "zimt", "pfeffer", "curry",
    "kurkuma", "muskat", "oregano", "thymian", "rosmarin", "basilikum", "petersilie",
    "schnittlauch", "dill", "kakao", "kaffee", "tee", "schokolade", "kuvertüre",
    "sesam", "mohn", "couscous", "bulgur", "quinoa", "polenta", "spinat", "rucola",
)


def plausible_plural(name, plural) -> str:
    """The suggested plural, or "" if it adds nothing (same as the singular)
    or makes no sense (German mass/uncountable ingredient)."""
    plural = plural.strip() if isinstance(plural, str) else ""
    if not plural or _same_word(plural, name):
        return ""
    if get_language_code(settings.output_language) == "de":
        last = (name or "").strip().split()[-1].lower() if (name or "").strip() else ""
        if last.endswith(_GERMAN_MASS_ENDINGS):
            return ""
    return plural


def _clean_nutrition(nutrition):
    if not isinstance(nutrition, dict):
        return None
    cleaned = {"basis": "ml" if nutrition.get("basis") == "ml" else "g"}
    for key in NUTRIENT_KEYS:
        value = nutrition.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            cleaned[key] = round(float(value), 1)
    return cleaned if len(cleaned) > 1 else None


def _describe_enrich(name, plural, nutrition, category):
    parts = []
    if category:
        parts.append(f"category {category['name']!r}")
    if plural:
        parts.append(f"plural {plural!r}")
    if nutrition:
        values = {
            "energy_kcal": "{} kcal", "protein_g": "{} g protein", "fat_g": "{} g fat", "carbs_g": "{} g carbs",
        }
        parts.append(", ".join(values[k].format(f"{nutrition[k]:g}") for k in values if k in nutrition)
                     + f" per 100 {nutrition['basis']}")
    return f"{name!r}: " + " · ".join(parts)


def enrich_targets(foods, categories, nutrition_available=True) -> list[dict]:
    """The subset of full food dicts that miss a plural, nutrition (only
    asked when matching property types exist in Tandoor), or (when any
    categories exist to pick from) a supermarket category. Nutrition and
    category are not asked for foods the user ignored for them in the
    health overview."""
    skip_nutrition = ignored.keys("foods_without_nutrition")
    skip_category = ignored.keys("foods_without_category")
    targets = []
    for food in foods:
        needs_plural = not (food.get("plural_name") or "").strip()
        needs_nutrition = nutrition_available and not food.get("properties") and str(food["id"]) not in skip_nutrition
        # No existing categories -> nothing to pick from, so never ask.
        needs_category = (bool(categories) and not food.get("supermarket_category")
                          and str(food["id"]) not in skip_category)
        if needs_plural or needs_nutrition or needs_category:
            targets.append({"id": food["id"], "name": food["name"], "needs_plural": needs_plural,
                            "needs_nutrition": needs_nutrition, "needs_category": needs_category})
    return targets


def enrich_suggestions(job, targets, categories) -> list[ToolSuggestion]:
    """Batched plural/nutrition/category lookup for enrich_targets() output.
    A proposed plural spelled like the singular is dropped, and only ids of
    existing categories are accepted. Adds token usage to `job`, stops early
    on cancel. Shared by the enrich tool and the new-recipes workflow."""
    categories_by_id = {c["id"]: c for c in categories}
    by_id = {t["id"]: t for t in targets}
    system_prompt = ENRICH_SYSTEM_PROMPT.replace("{language}", settings.output_language)
    suggestions = []
    chunks = list(chunked(targets, ENRICH_CHUNK_SIZE))
    for i, chunk in enumerate(chunks, 1):
        if job.cancel_requested:
            break
        job.progress_label = f"Checking ingredient details {i}/{len(chunks)}..."
        tool_jobs.save_tool_job(job)
        try:
            text_out, usage = llm_provider.complete_tool_text(
                system_prompt,
                json.dumps({"categories": categories if any(t["needs_category"] for t in chunk) else [],
                            "ingredients": chunk}, ensure_ascii=False),
                max_tokens=8000,
            )
            job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
            job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
            text_out = text_out.strip().strip("`")
            if text_out.startswith("json"):
                text_out = text_out[4:]
            answers = json.loads(text_out)
        except Exception as exc:  # noqa: BLE001
            log.warning("Ingredients enrich chunk %d failed: %s", i, exc)
            answers = []

        for answer in answers:
            target = by_id.get(answer.get("id")) if isinstance(answer, dict) else None
            if target is None:
                continue
            plural = plausible_plural(target["name"], answer.get("plural_name") if target["needs_plural"] else None)
            nutrition = _clean_nutrition(answer.get("nutrition")) if target["needs_nutrition"] else None
            # Only accept ids of categories that actually exist.
            category = categories_by_id.get(answer.get("category_id")) if target["needs_category"] else None
            if not plural and not nutrition and not category:
                continue
            suggestions.append(ToolSuggestion(
                id=uuid.uuid4().hex[:10], kind="enrich",
                summary=_describe_enrich(target["name"], plural, nutrition, category),
                detail={"food_id": target["id"], "plural_name": plural or None, "nutrition": nutrition,
                        "category": {"id": category["id"], "name": category["name"]} if category else None},
            ))
        job.progress_current = min(i * ENRICH_CHUNK_SIZE, len(targets))
        tool_jobs.save_tool_job(job)
    return suggestions


ENRICH_TILES = ("foods_without_nutrition", "foods_without_category")


def tile_food_ids() -> set[str] | None:
    """Ingredients listed in the overview's "without nutrition" and "without
    supermarket category" tiles (not the ignored ones) - None without an
    overview."""
    from . import health, ignored
    items = health._read().get("items")
    if not items or not any(m in items for m in ENRICH_TILES):
        return None
    ids = set()
    for metric in ENRICH_TILES:
        skip = ignored.keys(metric)
        ids |= {i["key"] for i in items.get(metric, []) if i["key"] not in skip}
    return ids


def run_enrich_scan(job_id: str) -> None:
    """Finds foods without a plural, nutrition and/or supermarket category
    and asks the AI for them in batches of ENRICH_CHUNK_SIZE foods per call.
    One suggestion per food."""
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        return
    try:
        if not llm_provider.is_configured():
            job.status = "error"
            job.error = llm_provider.missing_key_hint()
            tool_jobs.save_tool_job(job)
            return

        with tandoor_client.get_client() as client:
            job.progress_label = "Loading ingredients..."
            tool_jobs.save_tool_job(job)
            categories = fetch_supermarket_categories(client)
            try:
                nutrition_available = bool(nutrition_properties.existing_nutrient_types(client))
            except tandoor_client.TandoorError as exc:
                log.warning("Nutrition skipped - property types unavailable: %s", exc)
                nutrition_available = False
            foods = fetch_all_foods_full(client)
            listed = tile_food_ids() if job.meta.get("focus") == "tiles" else None
            if listed is not None:
                # Started from a tile: only the ingredients the overview
                # lists there, not every ingredient of the collection.
                foods = [f for f in foods if str(f["id"]) in listed]
            targets = enrich_targets(foods, categories, nutrition_available)
            job.progress_total = len(targets)
            job.cost_estimate = format_cost_estimate(len(targets), "chunked_enrich")
            tool_jobs.save_tool_job(job)

            job.suggestions = enrich_suggestions(job, targets, categories)
            job.status = "cancelled" if job.cancel_requested else "ready"
            job.progress_label = None
            tool_jobs.save_tool_job(job)

    except Exception as exc:  # noqa: BLE001
        log.exception("Ingredients enrich scan failed for job %s", job_id)
        job.status = "error"
        job.error = str(exc)
        tool_jobs.save_tool_job(job)


def apply_enrich_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    """Re-reads the food first and only fills fields that are STILL empty -
    never overwrites a plural or nutrition values set in the meantime."""
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise tandoor_client.TandoorError("Job not found.")
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise tandoor_client.TandoorError("Suggestion not found.")
    if suggestion.status != "pending":
        return suggestion

    detail = suggestion.detail
    try:
        with tandoor_client.get_client() as client:
            resp = client.get(f"/food/{detail['food_id']}/")
            if resp.status_code == 404:
                raise tandoor_client.TandoorError("This ingredient no longer exists (merged or deleted?).")
            resp.raise_for_status()
            food = resp.json()

            # "name" is always sent along: some Tandoor serializers read it
            # unconditionally on update and 500 without it (see units).
            payload = {"name": food["name"]}
            if detail.get("plural_name") and not (food.get("plural_name") or "").strip():
                payload["plural_name"] = detail["plural_name"]

            nutrition = detail.get("nutrition")
            if nutrition and not food.get("properties"):
                # Only property types that already exist ("Kalorien",
                # "Proteine", ...) - never create new ones.
                types = nutrition_properties.existing_nutrient_types(client)
                properties = []
                for key in NUTRIENT_KEYS:
                    pt = types.get(key)
                    if key in nutrition and pt is not None:
                        properties.append({
                            "property_type": {"id": pt["id"], "name": pt["name"]},
                            "property_amount": nutrition_properties.convert(nutrition[key], key, pt.get("unit")),
                        })
                if not properties:
                    raise tandoor_client.TandoorError(
                        "No matching nutrition property types exist in Tandoor (e.g. Kalorien, Proteine, Fett, "
                        "Kohlenhydrate) - create them there first; this tool never creates new ones."
                    )
                payload["properties"] = properties
                # The values are per 100 g/ml - tell Tandoor, unless the food
                # already has its own reference amount configured.
                if not food.get("properties_food_unit"):
                    unit_id, unit_name = tandoor_client._get_or_create(client, "unit", nutrition["basis"])
                    payload["properties_food_amount"] = 100
                    payload["properties_food_unit"] = {"id": unit_id, "name": unit_name}

            category = detail.get("category")
            if category and not food.get("supermarket_category"):
                # Re-check it still exists: Tandoor's food serializer does a
                # get-or-create BY NAME here, so a category deleted since the
                # scan would silently be re-created.
                resp = client.get(f"/{SUPERMARKET_CATEGORY_ENDPOINT}/{category['id']}/")
                if resp.status_code != 200:
                    raise tandoor_client.TandoorError(f"Supermarket category {category['name']!r} no longer exists - rescan.")
                payload["supermarket_category"] = {"id": category["id"], "name": resp.json()["name"]}

            if len(payload) > 1:
                resp = client.patch(f"/food/{detail['food_id']}/", json=payload)
                if resp.status_code not in (200, 201):
                    raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status = "error"
        suggestion.error = str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion
