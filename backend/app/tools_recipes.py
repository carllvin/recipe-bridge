"""Re-translates recipes ALREADY in Tandoor into OUTPUT_LANGUAGE - title,
description, step titles and instructions, and the recipe-specific
ingredient notes (e.g. "finely chopped"). Shared by the web UI tool
("recipes_translate") and scripts/retranslate_recipes.py, so both use
exactly the same prompt and payload logic.

Ingredient/unit/tag NAMES are deliberately left alone - those are shared
entities used by many other recipes too (the ingredients/units/tags tools
handle them)."""
from __future__ import annotations

import json
import logging
import uuid

from . import ignored, json_answer, llm_provider, recipe_scope, tandoor_client, tool_jobs
from .config import get_language_code, settings
from .schemas import ToolSuggestion
from .tandoor_helpers import format_cost_estimate, minimal_ref

log = logging.getLogger("tandoor-helper")

SYSTEM_PROMPT = """You translate cookbook recipe text into {language}.

You will receive a JSON object with:
- "title": string
- "description": string or null
- "steps": a list of {"title": string or null, "instruction": string}
- "ingredient_notes": an object mapping an opaque key (like "0.2") to the
  recipe-specific note of one ingredient (e.g. "finely chopped", "at room
  temperature")

Translate every text value naturally into {language}, preserving meaning and
a natural recipe-writing tone - not a literal word-for-word translation.
Text that is already in {language} stays as it is.

CRITICAL: some step instructions contain Jinja template placeholders like
"{{ ingredients[0] }}" or "{{ scale(200) }}" or Jinja comments like
"{# note #}". Leave every such {{ ... }} or {# ... #} block EXACTLY as
written, character for character, including its exact index number - only
translate the surrounding prose text around it.

Respond with ONLY a JSON object of the same shape - "title", "description",
"steps" (same number of steps, same order, each with "title" and
"instruction"; keep a null title null), "ingredient_notes" (exactly the same
keys) - no explanation, no markdown code fence.
"""

# Notes shorter than this (combined) are too little text for langdetect to
# judge reliably, so they don't force a translation on their own.
MIN_NOTES_TEXT_FOR_DETECTION = 20


def _ingredient_notes(recipe: dict) -> dict[str, str]:
    notes = {}
    for si, step in enumerate(recipe.get("steps", [])):
        for ii, ing in enumerate(step.get("ingredients", [])):
            note = (ing.get("note") or "").strip()
            if note:
                notes[f"{si}.{ii}"] = note
    return notes


def _shape(recipe: dict) -> list[int]:
    """Ingredient count per step - notes are matched by position, so the
    recipe must still have this exact shape when a suggestion is applied."""
    return [len(step.get("ingredients", [])) for step in recipe.get("steps", [])]


def recipe_text_blob(recipe: dict) -> str:
    parts = [recipe.get("name") or "", recipe.get("description") or ""]
    for step in recipe.get("steps", []):
        parts += [step.get("name") or "", step.get("instruction") or ""]
    return " ".join(p for p in parts if p).strip()


def _is_language(text: str, expected_code: str) -> bool:
    try:
        from langdetect import DetectorFactory, detect
        # langdetect is random by default - without a fixed seed a borderline
        # text could count as translated in the health tile and not in the
        # tool (or the other way round).
        DetectorFactory.seed = 0
        return detect(text) == expected_code
    except Exception:  # noqa: BLE001 - langdetect raises its own exception type for "can't tell"
        return False


def already_in_target_language(recipe: dict, expected_code: str | None) -> bool:
    """Best-effort language check via langdetect, so a recipe already in the
    target language costs no AI call at all. The recipe text and the
    ingredient notes are checked separately - a German recipe whose notes
    are still English ("finely chopped") would otherwise be detected as
    German overall and skipped. If langdetect can't tell (too little text,
    or OUTPUT_LANGUAGE isn't one it recognizes), this returns False - safer
    to translate unnecessarily than to silently skip a recipe that actually
    needed it."""
    if not expected_code:
        return False
    text = recipe_text_blob(recipe)
    if len(text) < 8 or not _is_language(text, expected_code):
        return False
    notes_text = " ".join(_ingredient_notes(recipe).values())
    if len(notes_text) >= MIN_NOTES_TEXT_FOR_DETECTION and not _is_language(notes_text, expected_code):
        return False
    return True


def translate_recipe_text(recipe: dict, language: str):
    payload = {
        "title": recipe.get("name", ""),
        "description": recipe.get("description"),
        "steps": [{"title": s.get("name") or None, "instruction": s.get("instruction", "")} for s in recipe.get("steps", [])],
        "ingredient_notes": _ingredient_notes(recipe),
    }
    # Plain replace, not str.format(): the prompt's own {{ ... }} / {# ... #}
    # Jinja examples would otherwise be misread as format fields.
    system_prompt = SYSTEM_PROMPT.replace("{language}", language)
    result, usage = json_answer.complete(
        lambda prompt: llm_provider.complete_text(prompt, json.dumps(payload, ensure_ascii=False), max_tokens=6000),
        system_prompt)

    steps = result.get("steps", [])
    if len(steps) != len(payload["steps"]):
        raise ValueError(
            f"Translation returned {len(steps)} step(s), expected {len(payload['steps'])} - "
            f"refusing to apply this (would misalign steps and their ingredients)."
        )
    # Tolerate the model answering with plain instruction strings.
    result["steps"] = [s if isinstance(s, dict) else {"title": None, "instruction": s} for s in steps]
    # Only keep notes for keys that were actually sent; missing ones keep
    # their original text.
    returned_notes = result.get("ingredient_notes") or {}
    result["ingredient_notes"] = {
        key: str(returned_notes[key]) for key in payload["ingredient_notes"] if returned_notes.get(key)
    }
    return result, usage


def build_update_payload(recipe: dict, translated: dict) -> dict:
    notes = translated.get("ingredient_notes") or {}
    new_steps = []
    for si, (step, new_step_text) in enumerate(zip(recipe.get("steps", []), translated["steps"])):
        new_step = dict(step)
        new_step["instruction"] = new_step_text.get("instruction") or step.get("instruction", "")
        if step.get("name"):
            new_step["name"] = (new_step_text.get("title") or step["name"])[:128]
        new_ingredients = []
        for ii, ing in enumerate(step.get("ingredients", [])):
            new_ing = dict(ing)
            # food/unit stay the same entities - only rebuilt as minimal refs
            # (sending Tandoor's full read-only fields back can trigger a 400).
            for field in ("food", "unit"):
                if ing.get(field) is not None:
                    new_ing[field] = minimal_ref(ing[field])
            key = f"{si}.{ii}"
            if key in notes:
                new_ing["note"] = notes[key][:256]
            new_ingredients.append(new_ing)
        new_step["ingredients"] = new_ingredients
        new_steps.append(new_step)

    return {
        "name": translated["title"][:128],
        "description": (translated.get("description") or "")[:512],
        "steps": new_steps,
    }


def describe_changes(recipe: dict, translated: dict) -> str:
    """Before/after lines for everything that changes - shown in the UI's
    expandable preview and printed by the CLI script."""
    lines = []

    def add(label, old, new):
        if (old or "") != (new or ""):
            lines.append(f"{label}: {old!r}\n    -> {new!r}")

    add("title", recipe.get("name", ""), translated["title"])
    add("description", recipe.get("description"), translated.get("description"))
    notes = translated.get("ingredient_notes") or {}
    for si, (step, new_step) in enumerate(zip(recipe.get("steps", []), translated["steps"])):
        if step.get("name"):
            add(f"step {si + 1} title", step["name"], new_step.get("title") or step["name"])
        add(f"step {si + 1}", step.get("instruction", ""), new_step.get("instruction"))
        for ii, ing in enumerate(step.get("ingredients", [])):
            key = f"{si}.{ii}"
            if key in notes:
                food = (ing.get("food") or {}).get("name", "?")
                add(f"note ({food})", ing.get("note"), notes[key])
    return "\n".join(lines)


def run_scan(job_id: str) -> None:
    """Runs in a background thread (see main.py). Translates every recipe not
    already in OUTPUT_LANGUAGE and turns each into one suggestion - nothing
    is written until that suggestion is applied."""
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        return
    try:
        if not llm_provider.is_configured():
            job.status = "error"
            job.error = llm_provider.missing_key_hint()
            tool_jobs.save_tool_job(job)
            return

        expected_code = get_language_code(settings.output_language)
        with tandoor_client.get_client() as client:
            job.progress_label = "Looking for recipes to translate..."
            tool_jobs.save_tool_job(job)
            needing = recipe_scope.recipes_for(
                client, "recipes_not_translated", lambda r: not already_in_target_language(r, expected_code),
                ignored.keys("recipes_not_translated"), job)
            job.progress_total = len(needing)
            job.cost_estimate = format_cost_estimate(len(needing), "per_recipe_translate")
            tool_jobs.save_tool_job(job)

            suggestions, skipped = [], []
            for i, recipe in enumerate(needing, 1):
                if job.cancel_requested:
                    break
                job.progress_current = i
                job.progress_label = f"Translating recipe {i}/{len(needing)}..."
                tool_jobs.save_tool_job(job)
                try:
                    translated, usage = translate_recipe_text(recipe, settings.output_language)
                    job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
                    job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
                except Exception as exc:  # noqa: BLE001
                    log.warning("Translation failed for recipe %s: %s", recipe.get("id"), exc)
                    skipped.append({"name": recipe.get("name", ""), "reason": "failed", "error": str(exc)[:300]})
                    continue
                preview = describe_changes(recipe, translated)
                if not preview:
                    # The AI found nothing to translate - the language check
                    # was wrong. Ignored in the tile, so it isn't listed again.
                    ignored.add("recipes_not_translated", [{"key": str(recipe["id"]), "name": recipe.get("name", "")}])
                    skipped.append({"name": recipe.get("name", ""), "reason": "unchanged"})
                    continue
                suggestions.append(ToolSuggestion(
                    id=uuid.uuid4().hex[:10], kind="translate_recipe",
                    summary=f"translate {recipe.get('name', '')!r} -> {translated['title']!r}",
                    detail={"recipe_id": recipe["id"], "translated": translated, "shape": _shape(recipe)},
                    preview=preview,
                ))

            job.suggestions = suggestions
            job.meta["skipped"] = skipped
            job.status = "cancelled" if job.cancel_requested else "ready"
            job.progress_label = None
            tool_jobs.save_tool_job(job)

    except Exception as exc:  # noqa: BLE001
        log.exception("Recipe translation scan failed for job %s", job_id)
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

    try:
        with tandoor_client.get_client() as client:
            recipe_id = suggestion.detail["recipe_id"]
            translated = suggestion.detail["translated"]
            resp = client.get(f"/recipe/{recipe_id}/")
            resp.raise_for_status()
            recipe = resp.json()  # fresh copy - the recipe may have been edited since the scan
            if _shape(recipe) != suggestion.detail["shape"]:
                raise tandoor_client.TandoorError("The recipe's steps/ingredients changed since the scan - rescan to translate it.")
            resp = client.patch(f"/recipe/{recipe_id}/", json=build_update_payload(recipe, translated))
            if resp.status_code not in (200, 201):
                raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status = "error"
        suggestion.error = str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion
