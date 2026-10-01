"""Recipe doctor: contradictions inside a recipe, found without AI - and only
for those, the AI proposes a fix.

findings() (local, cheap):
- "unused": an ingredient that no step mentions
- "missing": a step names an ingredient that isn't in the list (checked
  against the collection's ingredient names)
- "amount": an amount that can't be right (500 g salt for 4 servings)
- "time": the recipe's time doesn't fit the times in the method
  ("2 hours" in the text, 15 minutes in the recipe)

plan_suggestion() asks the AI once per such recipe for fixes from a small
catalog (step text, amount, add an ingredient, times). If it finds the
findings are false alarms, the recipe goes on the tile's ignore list (it
can be brought back there). Like "amounts into the steps", the AI's text
changes must keep every number of the original."""
from __future__ import annotations

import json
import logging
import re
import uuid

from . import cook_today, ignored, json_answer, llm_provider, recipe_scope, tandoor_client, tool_jobs
from .config import settings
from .schemas import ToolSuggestion
from .tandoor_helpers import format_cost_estimate, minimal_ref

log = logging.getLogger("recipe-bridge")

METRIC = "recipes_inconsistent"
MIN_TEXT = 60            # shorter methods say too little to judge "unused"
MIN_TEXT_MINUTES = 30    # time checks only for recipes that take a while
MIN_KEPT_RATIO = 0.8

GRAMS = {"g": 1, "gr": 1, "gramm": 1, "gram": 1, "grams": 1, "kg": 1000, "kilogramm": 1000, "ml": 1, "l": 1000,
         "liter": 1000, "litre": 1000, "el": 15, "esslöffel": 15, "tbsp": 15, "tl": 5, "teelöffel": 5, "tsp": 5}
SALT = ("salz", "salt", "sel", "sale", "sal")
SPICES = ("pfeffer", "pepper", "zimt", "cinnamon", "paprikapulver", "paprika edelsüß", "chili", "cayenne", "muskat",
          "nutmeg", "kreuzkümmel", "cumin", "kurkuma", "turmeric", "curry", "nelke", "clove", "piment", "kardamom")
LIMITS = [(SALT, 8, "salt"), (SPICES, 6, "spice")]  # grams per serving
MAX_PER_SERVING = 1500  # grams of anything

DURATION = re.compile(r"(\d+(?:[.,]\d+)?)(?:\s*(?:-|–|bis|to)\s*(\d+(?:[.,]\d+)?))?\s*"
                      r"(stunden|stunde|std|hours|hour|hrs|h|minuten|minute|min|minutes|mins)\b", re.I)


# ---------- finding (no AI) ----------

def _norm(text) -> str:
    return re.sub(r"[^\w]+", " ", (text or "").casefold()).strip()


def _ingredients(recipe):
    return [(n, ing) for n, step in enumerate(recipe.get("steps") or []) for ing in step.get("ingredients") or []
            if not ing.get("is_header") and (ing.get("food") or {}).get("name")]


def _text(recipe) -> str:
    return " ".join(s.get("instruction") or "" for s in recipe.get("steps") or [])


def _mentioned(food, words) -> bool:
    """The food (or its plural) is in the text - forgiving about endings and
    compounds ("Zwiebel"/"Zwiebeln", "Hähnchenbrust"/"Hähnchen")."""
    for name in (food.get("name"), food.get("plural_name")):
        for token in _norm(name).split():
            if len(token) < 3:
                continue
            stem = token[:max(4, len(token) - 2)]
            if any(w.startswith(stem) or (len(w) >= 5 and token.startswith(w[:max(5, len(w) - 2)])) for w in words):
                return True
    return False


def _grams(ing) -> float | None:
    if ing.get("no_amount") or not ing.get("amount"):
        return None
    factor = GRAMS.get(_norm((ing.get("unit") or {}).get("name")))
    return float(ing["amount"]) * factor if factor else None


def text_minutes(recipe) -> int:
    """The time the method talks about: per step the longest duration named."""
    total = 0
    for step in recipe.get("steps") or []:
        longest = 0
        for m in DURATION.finditer(step.get("instruction") or ""):
            value = float((m.group(2) or m.group(1)).replace(",", "."))
            hours = m.group(3).casefold().startswith(("st", "h"))
            longest = max(longest, value * 60 if hours else value)
        total += longest
    return int(total)


def findings(recipe, vocabulary=()) -> list[dict]:
    """vocabulary: the collection's ingredient names (for "missing")."""
    steps = recipe.get("steps") or []
    text = _text(recipe)
    if not steps or "{{" in text:
        return []  # templates name the ingredients by position - nothing to compare
    found = []
    words = _norm(text).split()
    ingredients = _ingredients(recipe)
    if len(text) >= MIN_TEXT:
        for _n, ing in ingredients:
            food = ing["food"]
            if cook_today._is_staple([food.get("name", ""), food.get("plural_name") or ""]):
                continue
            if not _mentioned(food, words):
                found.append({"kind": "unused", "text": f"{food['name']} is in no step"})
    own = {_norm(i["food"].get(k)) for _n, i in ingredients for k in ("name", "plural_name") if i["food"].get(k)}
    padded = f" {_norm(text)} "
    reported = set()
    for name in vocabulary:
        key = _norm(name)
        if len(key) < 5 or key in own or key in reported or cook_today._is_staple([name, ""]):
            continue
        if f" {key} " in padded and not any(key in o or o in key for o in own if len(o) >= 4):
            reported.add(key)
            found.append({"kind": "missing", "text": f"the method uses {name}, which isn't in the ingredients"})
    servings = recipe.get("servings") or 0
    for _n, ing in ingredients:
        grams = _grams(ing)
        if grams is None:
            continue
        name = _norm(ing["food"].get("name"))
        per = grams / servings if servings else None
        for words_, limit, label in LIMITS:
            if any(w in name for w in words_) and per is not None and per > limit:
                found.append({"kind": "amount", "text": f"{ing['amount']:g} {(ing.get('unit') or {}).get('name', '')} "
                              f"{ing['food']['name']} for {servings:g} servings is a lot of {label}"})
                break
        else:
            if per is not None and per > MAX_PER_SERVING:
                found.append({"kind": "amount", "text": f"{ing['amount']:g} {(ing.get('unit') or {}).get('name', '')} "
                              f"{ing['food']['name']} for {servings:g} servings"})
    said = text_minutes(recipe)
    total = (recipe.get("working_time") or 0) + (recipe.get("waiting_time") or 0)
    if said >= MIN_TEXT_MINUTES and total and total < said * 0.5:
        found.append({"kind": "time", "text": f"the method takes about {said} min, the recipe says {total} min"})
    return found


# ---------- the AI's fixes ----------

PROMPT = """You check a recipe written in {language} for contradictions a
program found. You will receive a JSON object:
{"title": string, "servings": number|null, "working_time": int|null, "waiting_time": int|null,
"steps": [{"n": int, "text": string, "ingredients": [{"key": string, "text": string}]}],
"findings": [string]}

For each finding decide whether it is a real mistake. Fix only real ones,
with as few changes as possible, using ONLY these fixes:
{"type": "step_text", "n": <step>, "text": <the whole new step text>}
  (e.g. mention an ingredient where it is used; keep everything else,
   every number, time and temperature)
{"type": "amount", "key": <ingredient key>, "amount": number, "unit": string|null}
  (an obvious typo like 500 g salt -> 5 g; unit null keeps the unit)
{"type": "add_ingredient", "n": <step>, "food": string, "amount": number|null, "unit": string|null}
  (an ingredient the method clearly uses but the list lacks)
{"type": "times", "working_time": int|null, "waiting_time": int|null}
  (minutes: active work / baking, simmering, resting)
A finding that is no mistake (e.g. "all ingredients" covers it, a seasoning
"to taste"): no fix - say so in "false_alarms".

Respond with ONLY a JSON object (no explanation, no markdown fence):
{"fixes": [<fix>, ...], "false_alarms": [<finding text>, ...]}
"""


def _numbers(text) -> list[str]:
    return re.findall(r"\d+(?:[.,]\d+)?", text or "")


def _keyed(recipe) -> dict:
    keys, n = {}, 0
    for step in recipe.get("steps") or []:
        for ing in step.get("ingredients") or []:
            if (ing.get("food") or {}).get("name") and not ing.get("is_header"):
                keys[f"i{n}"] = ing
                n += 1
    return keys


def _ing_text(ing) -> str:
    amount = "" if ing.get("no_amount") or not ing.get("amount") else f"{float(ing['amount']):g} "
    unit = ((ing.get("unit") or {}).get("name") or "")
    return f"{amount}{unit + ' ' if unit else ''}{ing['food']['name']}".strip()


def doctor_plan(job, recipe, found) -> dict:
    """{"fixes": [...validated...], "false_alarms": [...]}"""
    keys = _keyed(recipe)
    key_of = {id(ing): k for k, ing in keys.items()}
    steps = recipe.get("steps") or []
    payload = {
        "title": recipe.get("name", ""), "servings": recipe.get("servings") or None,
        "working_time": recipe.get("working_time") or None, "waiting_time": recipe.get("waiting_time") or None,
        "steps": [{"n": n, "text": s.get("instruction") or "",
                   "ingredients": [{"key": key_of[id(i)], "text": _ing_text(i)} for i in s.get("ingredients") or []
                                   if id(i) in key_of]} for n, s in enumerate(steps)],
        "findings": [f["text"] for f in found],
    }

    def ask(system_prompt):
        out, usage = llm_provider.complete_tool_text(system_prompt, json.dumps(payload, ensure_ascii=False),
                                                     max_tokens=3000)
        job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
        job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
        return out, usage
    answer = json_answer.complete(ask, PROMPT.replace("{language}", settings.output_language))[0]
    answer = answer if isinstance(answer, dict) else {}
    fixes = []
    for fix in answer.get("fixes") or []:
        if not isinstance(fix, dict):
            continue
        kind = fix.get("type")
        if kind == "step_text":
            n, text = fix.get("n"), str(fix.get("text") or "").strip()
            if not isinstance(n, int) or not 0 <= n < len(steps) or not text:
                continue
            old = steps[n].get("instruction") or ""
            if len(text) < MIN_KEPT_RATIO * len(old.strip()) or any(x not in _numbers(text) for x in _numbers(old)):
                log.info("Doctor: step %d text change not safe - dropped", n + 1)
                continue
            if text != old.strip():
                fixes.append({"type": "step_text", "n": n, "old": old, "text": text})
        elif kind == "amount" and fix.get("key") in keys and isinstance(fix.get("amount"), (int, float)) and fix["amount"] > 0:
            ing = keys[fix["key"]]
            unit = str(fix.get("unit") or "").strip() or None
            fixes.append({"type": "amount", "id": ing.get("id"), "old": _ing_text(ing), "amount": fix["amount"],
                          "unit": unit, "shown_unit": unit or (ing.get("unit") or {}).get("name") or "",
                          "food": ing["food"]["name"]})
        elif kind == "add_ingredient" and str(fix.get("food") or "").strip():
            n = fix.get("n") if isinstance(fix.get("n"), int) and 0 <= fix.get("n") < len(steps) else 0
            amount = fix.get("amount") if isinstance(fix.get("amount"), (int, float)) and fix["amount"] > 0 else None
            fixes.append({"type": "add_ingredient", "n": n, "food": str(fix["food"]).strip(), "amount": amount,
                          "unit": str(fix.get("unit") or "").strip() or None})
        elif kind == "times":
            times = {k: int(fix[k]) for k in ("working_time", "waiting_time")
                     if isinstance(fix.get(k), (int, float)) and 0 <= fix[k] < 60 * 48}
            if times:
                fixes.append({"type": "times", **times,
                              "old": f"{recipe.get('working_time') or 0} + {recipe.get('waiting_time') or 0} min"})
    return {"fixes": fixes, "false_alarms": [str(x) for x in answer.get("false_alarms") or []],
            "ids": sorted(str(i.get("id")) for i in keys.values())}


def describe(recipe, found, plan) -> tuple[str, str]:
    lines = ["FOUND:"] + [f"  • {f['text']}" for f in found] + ["FIX:"]
    for fix in plan["fixes"]:
        if fix["type"] == "step_text":
            lines += [f"  {fix['n'] + 1}. BEFORE: {fix['old']}", f"     AFTER:  {fix['text']}"]
        elif fix["type"] == "amount":
            unit = fix.get("shown_unit") or fix["unit"] or ""
            lines.append(f"  {fix['old']} -> {fix['amount']:g} {unit + ' ' if unit else ''}{fix['food']}")
        elif fix["type"] == "add_ingredient":
            amount = f"{fix['amount']:g} " if fix["amount"] else ""
            lines.append(f"  + {amount}{fix['unit'] + ' ' if fix['unit'] else ''}{fix['food']} (step {fix['n'] + 1})")
        elif fix["type"] == "times":
            lines.append(f"  time: {fix['old']} -> {fix.get('working_time', '-')} + {fix.get('waiting_time', '-')} min")
    if plan["false_alarms"]:
        lines += ["NO MISTAKE:"] + [f"  • {x}" for x in plan["false_alarms"]]
    return f"recipe: fix {recipe.get('name', '')!r} ({len(plan['fixes'])} change(s))", "\n".join(lines)


def plan_suggestion(job, recipe, vocabulary=()) -> ToolSuggestion | None:
    found = findings(recipe, vocabulary)
    if not found:
        return None
    try:
        plan = doctor_plan(job, recipe, found)
    except Exception as exc:  # noqa: BLE001
        log.warning("Doctor for recipe %s failed: %s", recipe.get("id"), exc)
        return None
    if not plan["fixes"]:
        # all false alarms: off the tile (can be brought back from its ignore list)
        ignored.add(METRIC, [{"key": str(recipe["id"]), "name": f"{recipe.get('name', '')} – OK (AI)"}])
        return None
    summary, preview = describe(recipe, found, plan)
    return ToolSuggestion(id=uuid.uuid4().hex[:10], kind="doctor_fix", summary=summary, preview=preview,
                          detail={"recipe_id": recipe["id"], "plan": plan})


def _check_unchanged(recipe, plan) -> None:
    if sorted(str(i.get("id")) for i in _keyed(recipe).values()) != plan["ids"]:
        raise tandoor_client.TandoorError("The recipe's ingredients changed since the check - check it again.")


# ---------- Tandoor ----------

def build_payload(client, recipe, plan) -> dict:
    _check_unchanged(recipe, plan)
    steps = []
    for old in recipe.get("steps") or []:
        step = dict(old)
        step["ingredients"] = []
        for ing in old.get("ingredients") or []:
            ing = dict(ing)
            for field in ("food", "unit"):
                if ing.get(field) is not None:
                    ing[field] = minimal_ref(ing[field])
            step["ingredients"].append(ing)
        steps.append(step)
    payload = {"steps": steps}

    def unit_ref(name):
        uid, uname = tandoor_client._get_or_create(client, "unit", name)
        return {"id": uid, "name": uname}
    for fix in plan["fixes"]:
        if fix["type"] == "step_text":
            steps[fix["n"]]["instruction"] = fix["text"]
        elif fix["type"] == "amount":
            for step in steps:
                for ing in step["ingredients"]:
                    if ing.get("id") == fix["id"]:
                        ing["amount"], ing["no_amount"] = fix["amount"], False
                        if fix["unit"]:
                            ing["unit"] = unit_ref(fix["unit"])
        elif fix["type"] == "add_ingredient":
            fid, fname = tandoor_client._get_or_create(client, "food", fix["food"])
            steps[min(fix["n"], len(steps) - 1)]["ingredients"].append({
                "food": {"id": fid, "name": fname}, "unit": unit_ref(fix["unit"]) if fix["unit"] else None,
                "amount": fix["amount"] or 0, "no_amount": fix["amount"] is None, "note": ""})
        elif fix["type"] == "times":
            payload.update({k: fix[k] for k in ("working_time", "waiting_time") if k in fix})
    return payload


def run_scan(job_id: str) -> None:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        return
    try:
        if not llm_provider.is_configured():
            job.status, job.error = "error", llm_provider.missing_key_hint()
            tool_jobs.save_tool_job(job)
            return
        with tandoor_client.get_client() as client:
            job.progress_label = "Looking for contradictions (no AI)..."
            tool_jobs.save_tool_job(job)
            vocabulary = [f["name"] for f in tandoor_client.fetch_all_items(client, "food")]
            candidates = recipe_scope.recipes_for(client, METRIC, lambda r: bool(findings(r, vocabulary)),
                                                  ignored.keys(METRIC), job)
        job.progress_total = len(candidates)
        job.cost_estimate = format_cost_estimate(len(candidates), "per_recipe_translate")
        tool_jobs.save_tool_job(job)
        suggestions = []
        for i, recipe in enumerate(candidates, 1):
            if job.cancel_requested:
                break
            job.progress_current = i
            job.progress_label = f"Checking {recipe.get('name', '')!r} ({i}/{len(candidates)})..."
            tool_jobs.save_tool_job(job)
            suggestion = plan_suggestion(job, recipe, vocabulary)
            if suggestion:
                suggestions.append(suggestion)
        job.suggestions = suggestions
        job.status = "cancelled" if job.cancel_requested else "ready"
        job.progress_label = None
        tool_jobs.save_tool_job(job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Recipe doctor failed for job %s", job_id)
        job.status, job.error = "error", str(exc)
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
            rid = suggestion.detail["recipe_id"]
            resp = client.get(f"/recipe/{rid}/")
            resp.raise_for_status()
            resp = client.patch(f"/recipe/{rid}/", json=build_payload(client, resp.json(), suggestion.detail["plan"]))
            if resp.status_code not in (200, 201):
                raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status, suggestion.error = "error", str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion
