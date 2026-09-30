"""Creates the unit conversions Tandoor needs to calculate a recipe's
nutrition: an ingredient's values are stored per 100 g (or ml), so every
ingredient line in another unit ("1 EL Olivenöl", "1 Prise Salz") needs a
conversion to that unit - otherwise Tandoor shows "?" for it.

Only ingredients that have nutrition values count (for the others a
conversion changes nothing). Per (ingredient, unit) pair without a usable
conversion:
- metric units of the same kind (kg, mg -> g; l, dl, cl -> ml) are
  converted in code - one general conversion per unit, no AI;
- everything else (EL, TL, Prise, Stück, Dose, ml of a food measured in
  g ...) is estimated by the AI, batched, as a conversion for that food.
Suggestions are applied after review, like the other tools."""
from __future__ import annotations

import logging
import uuid

from . import ignored, llm_provider, tandoor_client, tool_jobs, tools_ingredients, tools_tags
from .config import settings
from .schemas import ToolSuggestion
from .tandoor_helpers import chunked, fetch_all_recipes_full

log = logging.getLogger("recipe-bridge")

CONVERSION_ENDPOINT = "unit-conversion"
BATCH_SIZE = 40

# unit name (lowercase) -> (target unit name, factor): 1 <unit> = factor <target>
METRIC = {
    "kg": ("g", 1000), "kilogramm": ("g", 1000), "mg": ("g", 0.001), "milligramm": ("g", 0.001),
    "gramm": ("g", 1),
    "l": ("ml", 1000), "liter": ("ml", 1000), "dl": ("ml", 100), "cl": ("ml", 10),
    "milliliter": ("ml", 1),
}

WEIGHT_BASE_UNITS = {"g", "kg", "mg", "gram", "kilogram", "milligram", "ounce", "pound"}

ESTIMATE_SYSTEM_PROMPT = """You estimate kitchen unit conversions for a home
cook's recipe database (language: {language}). You will receive a JSON
array of {"id": integer, "food": string, "unit": string, "target": string}.

For each, answer how many <target> (g or ml) ONE <unit> of that food
typically is - e.g. 1 EL Olivenöl ~ 13 g, 1 TL Salz ~ 6 g, 1 Prise Salz
~ 0.5 g, 1 Stück Ei ~ 60 g, 1 Zehe Knoblauch ~ 4 g, 1 Dose gehackte
Tomaten ~ 400 g, 1 ml Milch ~ 1.03 g. Use typical average values, rounded
sensibly. Use null if the unit can't sensibly be converted for this food
(e.g. "nach Geschmack", "etwas").

Respond with ONLY a JSON array (no explanation, no markdown fence), one
element per input: {"id": <id>, "amount": <number or null>}
"""


def _key(name) -> str:
    return (name or "").strip().lower()


def _fetch_all(client, endpoint) -> list[dict]:
    items, url, params = [], f"/{endpoint}/", {"page_size": 200}
    for _ in range(200):
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("results", data) if isinstance(data, dict) else data)
        url = data.get("next") if isinstance(data, dict) else None
        if not url:
            break
        params = None
    return items


def _ref_id(ref):
    return (ref or {}).get("id")


def recipe_pairs(recipes) -> dict[tuple[int, int], int]:
    """(food id, unit id) -> number of recipes using that combination."""
    pairs = {}
    for recipe in recipes:
        seen = set()
        for step in recipe.get("steps", []):
            for ing in step.get("ingredients", []):
                pair = (_ref_id(ing.get("food")), _ref_id(ing.get("unit")))
                if None not in pair and pair not in seen:
                    seen.add(pair)
                    pairs[pair] = pairs.get(pair, 0) + 1
    return pairs


def find_missing(client, pairs, pending_nutrition=None, food_names=None,
                 respect_ignored=True) -> tuple[list[ToolSuggestion], list[dict]]:
    """The no-AI part: (ready general metric conversions, pairs that need an
    AI estimate). Also used by the health overview to count what's missing.

    pending_nutrition: food id -> "g"/"ml" for foods that don't have
    nutrition values yet but will once a pending suggestion is applied (they
    need conversions too). food_names: food id -> name to show/ask with
    (e.g. the name after a pending rename). respect_ignored: leave out the
    pairs the user ignored in the health overview (off for the overview
    itself, which filters them when read)."""
    skip = ignored.keys("missing_conversions") if respect_ignored else set()
    pending_nutrition = pending_nutrition or {}
    food_names = food_names or {}
    foods = {f["id"]: f for f in tools_ingredients.fetch_all_foods_full(client)}
    units = {u["id"]: u for u in _fetch_all(client, "unit")}
    units_by_name = {_key(u["name"]): u for u in units.values()}
    conversions = _fetch_all(client, CONVERSION_ENDPOINT)

    # Pairs (food, unit) that are convertible already, either way round.
    direct = set()   # (food_id or None, unit_a, unit_b)
    for c in conversions:
        a, b, food = _ref_id(c.get("base_unit")), _ref_id(c.get("converted_unit")), _ref_id(c.get("food"))
        direct.add((food, a, b))
        direct.add((food, b, a))

    def target_of(food):
        ref = food.get("properties_food_unit")
        if ref:
            return units.get(_ref_id(ref))
        return units_by_name.get(pending_nutrition.get(food["id"], "g"))

    def convertible(food, unit, target):
        if unit["id"] == target["id"]:
            return True
        if (food["id"], unit["id"], target["id"]) in direct or (None, unit["id"], target["id"]) in direct:
            return True
        # Tandoor converts between units that have a base unit set
        # (metric/imperial) on its own - but only weight<->weight or
        # volume<->volume; ml -> g needs a food-specific conversion.
        a, b = _key(unit.get("base_unit")), _key(target.get("base_unit"))
        return bool(a) and bool(b) and ((a in WEIGHT_BASE_UNITS) == (b in WEIGHT_BASE_UNITS))

    suggestions, general_done, to_estimate = [], set(), []
    for (food_id, unit_id), count in sorted(pairs.items(), key=lambda kv: -kv[1]):
        food, unit = foods.get(food_id), units.get(unit_id)
        if not food or not unit or not (food.get("properties") or food_id in pending_nutrition):
            continue  # no nutrition values -> a conversion wouldn't change anything
        target = target_of(food)
        if target is None or convertible(food, unit, target):
            continue
        metric = METRIC.get(_key(unit["name"]))
        if metric and _key(metric[0]) == _key(target["name"]):
            if unit_id not in general_done and f"*:{unit_id}" not in skip:
                general_done.add(unit_id)
                suggestions.append(ToolSuggestion(
                    id=uuid.uuid4().hex[:10], kind="conversion",
                    summary=f"all ingredients: 1 {unit['name']} = {metric[1]:g} {target['name']}",
                    detail={"food": None, "base_unit": {"id": unit["id"], "name": unit["name"]},
                            "converted_unit": {"id": target["id"], "name": target["name"]},
                            "converted_amount": metric[1]},
                ))
            continue
        if f"{food_id}:{unit_id}" in skip:
            continue
        name = food_names.get(food_id, food["name"])
        to_estimate.append({"food": {"id": food["id"], "name": name}, "unit": unit, "target": target, "count": count})
    return suggestions, to_estimate


def conversion_suggestions(job, client, pairs, pending_nutrition=None, food_names=None) -> list[ToolSuggestion]:
    """Conversion suggestions for the given (food, unit) pairs - see the
    module docstring. Shared by this tool and the new-recipes workflow."""
    suggestions, to_estimate = find_missing(client, pairs, pending_nutrition, food_names)
    job.progress_total = len(to_estimate)
    job.cost_estimate = job.cost_estimate or (
        f"{len(to_estimate)} ingredient/unit pair(s) to estimate -> "
        f"~{-(-len(to_estimate) // BATCH_SIZE)} small AI call(s) with the tools model.")
    tool_jobs.save_tool_job(job)

    prompt = ESTIMATE_SYSTEM_PROMPT.replace("{language}", settings.output_language)
    for b, batch in enumerate(chunked(to_estimate, BATCH_SIZE), 1):
        if job.cancel_requested:
            break
        job.progress_label = f"Estimating conversions, batch {b}..."
        job.progress_current = min(b * BATCH_SIZE, len(to_estimate))
        tool_jobs.save_tool_job(job)
        try:
            answers = tools_tags._complete_json(
                job, prompt,
                [{"id": i, "food": p["food"]["name"], "unit": p["unit"]["name"], "target": p["target"]["name"]}
                 for i, p in enumerate(batch)],
                max_tokens=25 * len(batch) + 100,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Conversion batch %d failed: %s", b, exc)
            continue
        amounts = {a.get("id"): a.get("amount") for a in answers if isinstance(a, dict)}
        for i, p in enumerate(batch):
            amount = amounts.get(i)
            if not isinstance(amount, (int, float)) or amount <= 0:
                continue
            amount = round(float(amount), 2)
            suggestions.append(ToolSuggestion(
                id=uuid.uuid4().hex[:10], kind="conversion",
                summary=(f"{p['food']['name']}: 1 {p['unit']['name']} = {amount:g} {p['target']['name']} "
                         f"(used in {p['count']} recipe(s))"),
                detail={"food": p["food"],
                        "base_unit": {"id": p["unit"]["id"], "name": p["unit"]["name"]},
                        "converted_unit": {"id": p["target"]["id"], "name": p["target"]["name"]},
                        "converted_amount": amount},
            ))
    return suggestions


def _pairs_since_overview(client, job) -> dict | None:
    """The (ingredient, unit) pairs the health overview counted, plus those
    of recipes new or changed since - so only those recipes are read.
    None without a usable overview (then every recipe is read)."""
    from . import recipe_scope
    stored = recipe_scope._health().get("recipe_pairs")
    if stored is None:
        return None
    known = recipe_scope.changed_since_overview(client, job)
    if known is None:
        return None
    pairs = {(f, u): n for f, u, n in stored}
    for pair, n in recipe_pairs(known[1]).items():
        pairs[pair] = max(pairs.get(pair, 0), n)
    return pairs


def run_scan(job_id: str) -> None:
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
            pairs = _pairs_since_overview(client, job)
            if pairs is None:
                job.progress_label = "Scanning every recipe's ingredients..."
                tool_jobs.save_tool_job(job)
                pairs = recipe_pairs(fetch_all_recipes_full(client))
            job.suggestions = conversion_suggestions(job, client, pairs)
            job.status = "cancelled" if job.cancel_requested else "ready"
            job.progress_label = None
            tool_jobs.save_tool_job(job)

    except Exception as exc:  # noqa: BLE001
        log.exception("Conversion scan failed for job %s", job_id)
        job.status = "error"
        job.error = str(exc)
        tool_jobs.save_tool_job(job)


def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise tandoor_client.TandoorError("Job not found.")
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise tandoor_client.TandoorError("Suggestion not found.")
    if suggestion.status != "pending":
        return suggestion

    d = suggestion.detail
    try:
        with tandoor_client.get_client() as client:
            # Skip if an equivalent conversion appeared in the meantime.
            for c in _fetch_all(client, CONVERSION_ENDPOINT):
                units = {_ref_id(c.get("base_unit")), _ref_id(c.get("converted_unit"))}
                if units == {d["base_unit"]["id"], d["converted_unit"]["id"]} and _ref_id(c.get("food")) == _ref_id(d["food"]):
                    suggestion.status = "applied"
                    return suggestion
            payload = {
                "base_amount": 1,
                "base_unit": d["base_unit"],
                "converted_amount": d["converted_amount"],
                "converted_unit": d["converted_unit"],
                "food": d["food"],
            }
            resp = client.post(f"/{CONVERSION_ENDPOINT}/", json=payload)
            if resp.status_code not in (200, 201):
                raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status = "error"
        suggestion.error = str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion
