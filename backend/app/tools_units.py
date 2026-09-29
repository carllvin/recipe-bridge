from __future__ import annotations

import json
import logging
import uuid

from . import duplicates, llm_provider, tandoor_client, tool_jobs
from .config import settings
from .schemas import ToolSuggestion
from .tandoor_helpers import chunked, delete_entity, entity_exists, fetch_all_recipes_full, find_recipes_using_unit, format_cost_estimate, minimal_ref, resolve_name_collisions, validate_actions

log = logging.getLogger("recipe-bridge")

REVIEW_SYSTEM_PROMPT = """You are cleaning up a home cook's unit-of-measure
database. Its target language is {language}. You will receive a JSON array
of existing unit entries, each {{"id": integer, "name": string,
"plural_name": string|null}}.

Find three kinds of problems:
1. Units that are really the same measure written differently - "TL",
   "Teelöffel", "tsp" all mean teaspoon; "EL", "Esslöffel", "tbsp" all mean
   tablespoon; "g" and "Gramm" mean the same thing but are still worth
   keeping SEPARATE in Tandoor's convention of short abbreviations for
   common units (g, ml, kg, l) - only flag those if BOTH forms exist for the
   exact same thing.
2. Any unit name that is not already in {language} - propose a {language}
   equivalent, merging with an existing matching entry if one exists.
3. A unit whose "plural_name" is null AND whose plural genuinely differs
   from the singular in {language} (e.g. "cup" -> "cups", "Dose" -> "Dosen")
   - propose that plural. Skip units that don't meaningfully pluralize (g,
   ml, kg, l, Stück/pcs, TL, EL and similar abbreviations) - if the plural
   would be spelled identically to the singular, don't propose it at all.

Prefer keeping the entry that is already the clean, conventional
abbreviated/short form used in {language} recipes; if none of a group is
already in {language}, invent the keep_name yourself.

Every id may appear in AT MOST ONE element of your answer - put all ids
of one unit into a single merge instead of listing a rename and a merge
for the same entry.

Only include entries that actually need a change. Respond with ONLY a JSON
array (no explanation, no markdown fence), each element one of:

{{"type": "rename", "id": <id>, "new_name": <corrected name>}}
{{"type": "merge", "keep_id": <id to keep>, "keep_name": <possibly corrected name for it>, "remove_ids": [<other ids that mean the same unit>]}}
{{"type": "set_plural", "id": <id>, "plural_name": <plural form>}}

If nothing needs a change, respond with [].
"""


def _fetch_units_with_plural(client, units):
    enriched = []
    for unit in units:
        resp = client.get(f"/unit/{unit['id']}/")
        plural_name = None
        if resp.status_code == 200:
            plural_name = resp.json().get("plural_name")
        enriched.append({**unit, "plural_name": plural_name})
    return enriched


def _describe(action, by_id):
    if action["type"] == "rename":
        old = by_id.get(action["id"], {}).get("name", "?")
        return f"rename {old!r} -> {action['new_name']!r}"
    if action["type"] == "set_plural":
        name = by_id.get(action["id"], {}).get("name", "?")
        return f"set plural of {name!r} -> {action['plural_name']!r}"
    keep_old = by_id.get(action["keep_id"], {}).get("name", "?")
    remove_names = ", ".join(f"{by_id.get(rid, {}).get('name', '?')!r}" for rid in action["remove_ids"])
    return f"merge {remove_names} into {keep_old!r} -> {action['keep_name']!r}"


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
            units = tandoor_client.fetch_all_items(client, "unit")
            job.progress_total = len(units)
            job.cost_estimate = format_cost_estimate(len(units), "chunked_review")
            tool_jobs.save_tool_job(job)
            units = _fetch_units_with_plural(client, units)
            all_units = units

            if job.meta.get("focus") == "duplicates":
                # Only the likely duplicates from the health overview, each
                # group kept together in one chunk.
                by_id = {u["id"]: u for u in units}
                groups = duplicates.open_duplicate_ids(duplicates.unit_duplicates(units), "units_duplicates")
                chunks = [[by_id[i] for i in chunk] for chunk in duplicates.pack_groups(groups, 80)]
                units = [u for chunk in chunks for u in chunk]
                job.progress_total = len(units)
                job.cost_estimate = format_cost_estimate(len(units), "chunked_review")
                tool_jobs.save_tool_job(job)
            else:
                chunks = list(chunked(units, 80))

            all_actions = []
            for i, chunk in enumerate(chunks, 1):
                if job.cancel_requested:
                    break
                job.progress_label = f"Reviewing chunk {i}/{len(chunks)}..."
                tool_jobs.save_tool_job(job)
                try:
                    system_prompt = REVIEW_SYSTEM_PROMPT.replace("{language}", settings.output_language)
                    text_out, usage = llm_provider.complete_tool_text(
                        system_prompt,
                        json.dumps(
                            [{"id": u["id"], "name": u["name"], "plural_name": u.get("plural_name")} for u in chunk],
                            ensure_ascii=False,
                        ),
                        max_tokens=8000,
                    )
                    text_out = text_out.strip().strip("`")
                    if text_out.startswith("json"):
                        text_out = text_out[4:]
                    all_actions.extend(validate_actions(json.loads(text_out), "units_review"))
                    job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
                    job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
                except Exception as exc:  # noqa: BLE001
                    log.warning("Unit review chunk %d failed: %s", i, exc)
                job.progress_current = min(sum(len(c) for c in chunks[:i]), len(units))
                tool_jobs.save_tool_job(job)

            all_actions = duplicates.drop_ignored_merges(resolve_name_collisions(all_actions, all_units), "units_duplicates")
            by_id = {u["id"]: u for u in all_units}

            # Drop no-op plurals: if the AI proposed a plural identical to the
            # singular (e.g. "Zucker" -> "Zucker"), there's nothing to set.
            all_actions = [
                a for a in all_actions
                if not (a["type"] == "set_plural"
                        and a["plural_name"].strip().lower() == by_id.get(a["id"], {}).get("name", "").strip().lower())
            ]

            job.suggestions = [
                ToolSuggestion(id=uuid.uuid4().hex[:10], kind=action["type"], summary=_describe(action, by_id), detail=action)
                for action in all_actions
            ]
            job.status = "cancelled" if job.cancel_requested else "ready"
            job.progress_label = None
            tool_jobs.save_tool_job(job)

    except Exception as exc:  # noqa: BLE001
        log.exception("Unit review scan failed for job %s", job_id)
        job.status = "error"
        job.error = str(exc)
        tool_jobs.save_tool_job(job)


def _apply_rename(client, unit_id, new_name):
    resp = client.patch(f"/unit/{unit_id}/", json={"name": new_name})
    if resp.status_code not in (200, 201):
        raise tandoor_client.TandoorError(f"Could not rename unit #{unit_id}: {resp.status_code} {resp.text[:300]}")


def _apply_set_plural(client, unit_id, plural_name):
    """Tandoor's UnitSerializer.update() reads validated_data['name']
    unconditionally, so a PATCH carrying only plural_name crashes it with a
    500 - always send the unit's current name along. Also checks live for
    another unit already NAMED like the plural, which would otherwise
    surface as the same opaque 500 from the uniqueness constraint."""
    resp = client.get(f"/unit/{unit_id}/")
    if resp.status_code == 404:
        raise tandoor_client.TandoorError(f"Unit #{unit_id} no longer exists (merged away by another suggestion?).")
    resp.raise_for_status()
    current_name = resp.json()["name"]

    wanted = plural_name.strip().lower()
    for other in tandoor_client.fetch_all_items(client, "unit"):
        if other["id"] == unit_id:
            continue
        if (other.get("name") or "").strip().lower() == wanted:
            raise tandoor_client.TandoorError(
                f"Another unit (#{other['id']} {other['name']!r}) already uses {plural_name!r} - merge them instead (rescan)."
            )

    resp = client.patch(f"/unit/{unit_id}/", json={"name": current_name, "plural_name": plural_name})
    if resp.status_code not in (200, 201):
        raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")


def _repoint_recipe_unit(recipe_detail, remove_id, keep_id, keep_name):
    new_steps = []
    for step in recipe_detail.get("steps", []):
        new_ingredients = []
        for ing in step.get("ingredients", []):
            new_ing = dict(ing)
            unit_ref = ing.get("unit")
            if unit_ref and unit_ref.get("id") == remove_id:
                new_ing["unit"] = {"id": keep_id, "name": keep_name}
            elif unit_ref is not None:
                new_ing["unit"] = minimal_ref(unit_ref)
            if ing.get("food") is not None:
                new_ing["food"] = minimal_ref(ing["food"])
            new_ingredients.append(new_ing)
        new_step = dict(step)
        new_step["ingredients"] = new_ingredients
        new_steps.append(new_step)
    return {"steps": new_steps}


def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
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
                _apply_rename(client, action["id"], action["new_name"])

            elif action["type"] == "set_plural":
                _apply_set_plural(client, action["id"], action["plural_name"])

            else:  # merge
                keep_id, keep_name = action["keep_id"], action["keep_name"]
                if not entity_exists(client, "unit", keep_id):
                    raise tandoor_client.TandoorError(
                        f"Unit #{keep_id} no longer exists (already merged by another suggestion?) - rescan to continue."
                    )
                _apply_rename(client, keep_id, keep_name)

                # No confirmed /recipe/?units=<id> filter, so scan once for this apply.
                all_recipes = fetch_all_recipes_full(client)

                for remove_id in action["remove_ids"]:
                    if not entity_exists(client, "unit", remove_id):
                        continue  # already merged away by an earlier suggestion
                    affected = find_recipes_using_unit(all_recipes, remove_id)
                    for recipe in affected:
                        resp = client.get(f"/recipe/{recipe['id']}/")
                        resp.raise_for_status()
                        payload = _repoint_recipe_unit(resp.json(), remove_id, keep_id, keep_name)
                        resp = client.patch(f"/recipe/{recipe['id']}/", json=payload)
                        if resp.status_code not in (200, 201):
                            raise tandoor_client.TandoorError(
                                f"Could not update recipe {recipe['id']}: {resp.status_code} {resp.text[:300]}"
                            )

                    # Verify against FRESH copies of the recipes just touched,
                    # not the (now stale) all_recipes snapshot.
                    still_used = []
                    for recipe in affected:
                        resp = client.get(f"/recipe/{recipe['id']}/")
                        if resp.status_code != 200:
                            continue
                        fresh = resp.json()
                        for step in fresh.get("steps", []):
                            for ing in step.get("ingredients", []):
                                unit = ing.get("unit")
                                if unit and unit.get("id") == remove_id:
                                    still_used.append(recipe)
                                    break
                    if still_used:
                        raise tandoor_client.TandoorError(
                            f"Still used by {len(still_used)} recipe(s) after repointing - not deleting #{remove_id}."
                        )

                    delete_entity(client, "unit", remove_id)

        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status = "error"
        suggestion.error = str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion
