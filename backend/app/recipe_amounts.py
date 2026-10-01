"""Amounts in the method: "Das Mehl mit der Milch verrühren" becomes
"250 g Mehl mit 500 ml Milch verrühren", so nobody has to look back at the
ingredient list while cooking.

The AI never writes a number: it only puts a marker ([[i0]]) where an
ingredient of that step is mentioned. The markers become
- Tandoor: its template "{{ ingredients[n] }}" (n = the ingredient's place
  in the step) - Tandoor shows the amount from the ingredient list, scaled
  when the servings are changed;
- Mealie (no templates): the amount written out ("250 g Mehl") - right for
  the recipe's own servings.

needs_amounts() decides locally (no AI): steps that mention hardly any of
their ingredients' amounts. Recipes that still need the structure revision
(all ingredients in step 1, one long block) wait for that first; Tandoor
recipes already using templates are left alone.

Used by the "Recipes: amounts into the steps" tile (Tandoor: run_scan /
apply_suggestion below; Mealie: mealie_tools)."""
from __future__ import annotations

import json
import logging
import re
import uuid

from . import ignored, json_answer, llm_provider, recipe_restructure, recipe_scope, tandoor_client, tool_jobs
from .config import get_language_code, settings
from .schemas import ToolSuggestion
from .tandoor_helpers import format_cost_estimate, minimal_ref

log = logging.getLogger("recipe-bridge")

METRIC = "recipes_amounts_missing"
MARK = re.compile(r"\[\[(i\d+)\]\]")
MIN_KEPT_RATIO = 0.8  # the method written out must keep at least this much of the old text

SYSTEM_PROMPT = """You add the ingredient amounts to the method of a recipe
written in {language}. You will receive a JSON object:
{"steps": [{"instruction": string, "ingredients": [{"key": string, "text": string}]}]}
where "text" is how the ingredient is shown with its amount and, in
brackets, its comment (e.g. "250 g Mehl", "2 Zwiebeln (fein gewürfelt)").

For every step, rewrite its instruction so that the FIRST mention of each
of that step's ingredients is replaced by the marker [[key]] - the marker
is later shown as the full "text". Adjust articles and grammar only as far
as needed, e.g. "Das Mehl mit der Milch verrühren" -> "[[i0]] mit [[i1]]
verrühren", "Die Zwiebel fein würfeln" -> "[[i2]] fein würfeln". Where
the bracketed comment already says what the sentence says, drop those words
from the sentence: "Die gewürfelten Zwiebeln andünsten" with "2 Zwiebeln
(gewürfelt)" -> "[[i3]] andünsten".

Rules:
- use only the keys of that same step, each at most once
- an ingredient the step doesn't mention: add it only where the step
  clearly uses it (e.g. "Alles verrühren" -> "[[i0]] und [[i1]] verrühren"),
  otherwise leave it out
- never write an amount or unit yourself, and change nothing else: keep
  the wording, times, temperatures, order and line breaks
- a step without ingredients stays exactly as it is

Respond with ONLY a JSON object (no explanation, no markdown fence):
{"steps": [<instruction>, ...]} - one string per step, same order and count.
"""


# ---------- reading ----------

def _number(value) -> str:
    text = f"{round(float(value), 2):g}"
    return text if get_language_code(settings.output_language) == "en" else text.replace(".", ",")


def _note(ing) -> str:
    """The ingredient's comment ("fein gewürfelt") - shown in brackets
    after it. Not for a line without a food, whose text is the note."""
    return (ing.get("note") or "").strip() if (ing.get("food") or {}).get("name") else ""


def amount_text(ing) -> str:
    """How an ingredient reads in the text: "250 g Mehl", "2 Eier", "Salz",
    "2 Zwiebeln (fein gewürfelt)"."""
    food = ing.get("food") or {}
    amount = None if ing.get("no_amount") else ing.get("amount")
    unit = (ing.get("unit") or {}).get("name") or ""
    name = food.get("name") or (ing.get("note") or "").strip()
    if amount and float(amount) > 1 and not unit and food.get("plural_name"):
        name = food["plural_name"]
    text = " ".join(p for p in (_number(amount) if amount else "", unit, name) if p)
    return f"{text} ({_note(ing)})" if _note(ing) else text


def _has_amount(ing) -> bool:
    return bool(ing.get("food") and ing.get("amount") and not ing.get("no_amount"))


def _mentions_amount(ing, text) -> bool:
    number = f"{round(float(ing['amount']), 2):g}"
    return any(re.search(rf"(?<![\d.,]){re.escape(n)}(?![\d.,]*\d)", text)
               for n in (number, number.replace(".", ",")))


TEMPLATE = re.compile(r"\{\{\s*ingredients\[(\d+)\]\s*\}\}")


def _notes_missing(step) -> list[int]:
    """Places n of a step whose "{{ ingredients[n] }}" doesn't show the
    ingredient's comment yet (recipes done before comments were added)."""
    text, ingredients = step.get("instruction") or "", step.get("ingredients") or []
    return [n for n in dict.fromkeys(int(m.group(1)) for m in TEMPLATE.finditer(text))
            if n < len(ingredients) and _note(ingredients[n]) and not _without_amount(ingredients[n])
            and f"ingredients[{n}].note" not in text]


def _without_amount(ing) -> bool:
    return bool(ing.get("no_amount") or not ing.get("amount"))


# "{{ ingredients[n] }}", optionally followed by " ({{ ingredients[n].note }})"
TEMPLATE_WITH_NOTE = re.compile(r"\{\{\s*ingredients\[(\d+)\]\s*\}\}(?:\s*\(\{\{\s*ingredients\[\1\]\.note\s*\}\}\))?")


def _zero_templates(step) -> list[int]:
    """Places n whose template would read "0 Pfeffer": Tandoor shows the
    amount 0 of an ingredient without an amount - those get the plain name."""
    text, ingredients = step.get("instruction") or "", step.get("ingredients") or []
    return [n for n in dict.fromkeys(int(m.group(1)) for m in TEMPLATE.finditer(text))
            if n < len(ingredients) and _without_amount(ingredients[n])]


def needs_amounts(recipe) -> bool:
    steps = recipe.get("steps") or []
    if steps and any("{{" in (s.get("instruction") or "") for s in steps):
        # templates already: only the comments and the "0 Pfeffer" ones
        return any(_notes_missing(s) or _zero_templates(s) for s in steps)
    if not steps:
        return False
    if recipe_restructure.needs_restructure(recipe):
        return False  # the structure revision comes first
    rows = [(ing, s.get("instruction") or "") for s in steps for ing in s.get("ingredients") or []
            if _has_amount(ing) and (s.get("instruction") or "").strip()]
    if not rows:
        return False
    mentioned = sum(1 for ing, text in rows if _mentions_amount(ing, text))
    return mentioned * 2 < len(rows)


# ---------- the AI's plan ----------

def _numbers(text) -> list[str]:
    return re.findall(r"\d+(?:[.,]\d+)?", text or "")


def amounts_plan(job, recipe) -> dict:
    """One AI call. Returns {"steps": [{"index", "text", "ids"}]} for the
    steps that change; raises ValueError for an answer that can't be
    trusted."""
    steps = recipe.get("steps") or []
    keyed = []  # per step: {key: ingredient}
    n = 0
    for step in steps:
        own = {}
        for ing in step.get("ingredients") or []:
            if ing.get("food") or (ing.get("note") or "").strip():
                own[f"i{n}"] = ing
                n += 1
        keyed.append(own)
    payload = {"steps": [{"instruction": s.get("instruction") or "",
                          "ingredients": [{"key": k, "text": amount_text(i)} for k, i in own.items()]}
                         for s, own in zip(steps, keyed)]}

    def ask(system_prompt):
        out, usage = llm_provider.complete_text(system_prompt, json.dumps(payload, ensure_ascii=False), max_tokens=6000)
        job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
        job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
        return out, usage
    answer = json_answer.complete(ask, SYSTEM_PROMPT.replace("{language}", settings.output_language))[0]
    texts = answer.get("steps") if isinstance(answer, dict) else None
    if not isinstance(texts, list) or len(texts) != len(steps) or not all(isinstance(t, str) for t in texts):
        raise ValueError("the answer doesn't have one text per step")

    changed = []
    for index, (step, own, text) in enumerate(zip(steps, keyed, texts)):
        old = step.get("instruction") or ""
        text, seen = _checked(old, text, own, index)
        if not seen or text == old:
            continue
        changed.append({"index": index, "text": text, "keys": {k: own[k].get("id") for k in seen},
                        "ids": [i.get("id") for i in step.get("ingredients") or []], "own": own, "original": old})
    _proofread(job, changed)
    for change in changed:
        del change["own"], change["original"]
    return {"steps": changed}


def _checked(old, text, own, index) -> tuple[str, set]:
    """The AI's marked text made safe: only this step's markers, each once;
    articles before a marker dropped; raises ValueError when text or
    numbers of the original got lost."""
    seen = set()

    def keep(match):
        key = match.group(1)
        if key not in own or key in seen:  # another step's or a repeated marker: the plain name
            return ((own.get(key) or {}).get("food") or {}).get("name", "") if key in own else ""
        seen.add(key)
        return match.group(0)
    text = _drop_articles(MARK.sub(keep, text))
    text = re.sub(r"(?<=\S)  +(?=\S)", " ", text).strip()
    if seen:
        written = MARK.sub(lambda m: amount_text(own[m.group(1)]), text)
        if len(written) < MIN_KEPT_RATIO * len(old.strip()):
            raise ValueError(f"step {index + 1} lost text")
        missing = [x for x in _numbers(old) if x not in _numbers(written)]
        if missing:
            raise ValueError(f"step {index + 1} lost the numbers {missing}")
    return text, seen


# ---------- does the text still read well? ----------

# An article right before an ingredient that brings its own amount reads
# double ("die 250 g Mehl") - dropped without asking.
ARTICLE = re.compile(r"\b(?:der|die|das|den|dem|des|ein|eine|einen|einem|einer|the|an?|le|la|les|il|lo|gli|el|los|las)\s+(?=\[\[i\d+\]\])",
                     re.I)
# German prepositions that want the dative - "mit 3 Eier" should be "mit 3 Eiern".
DATIVE = ("mit", "von", "aus", "zu", "nach", "bei", "samt", "nebst")


def _drop_articles(text) -> str:
    return ARTICLE.sub("", text)


def local_issues(text, own) -> list[str]:
    """What reads wrong in the written-out text, found without AI: a plural
    ingredient without a unit after a dative preposition ("mit 3 Eier")."""
    if get_language_code(settings.output_language) != "de":
        return []
    issues = []
    for m in MARK.finditer(text):
        ing = own.get(m.group(1))
        before = re.findall(r"[\wäöüß]+", text[:m.start()].casefold())
        if not ing or not before or before[-1] not in DATIVE:
            continue
        shown = amount_text({**ing, "note": ""})
        last = shown.split()[-1]
        amount = None if ing.get("no_amount") else ing.get("amount")
        plural = amount and float(amount) > 1 and not (ing.get("unit") or {}).get("name")
        if plural and not last.casefold().endswith(("n", "s")):
            issues.append(f"„{before[-1]} {shown}“")
    return issues


PROOFREAD_PROMPT = """You proofread cooking instructions in {language} into which
ingredient amounts were just inserted. You will receive a JSON object:
{"steps": [{"original": string, "marked": string, "reads_as": string,
"ingredients": [{"key": string, "text": string}]}]}
"marked" is the new text with markers like [[i0]]; "reads_as" is how it reads
once each marker is replaced by its ingredient "text" - which can't be
inflected (it always reads exactly like that).

For each step decide whether "reads_as" is correct, natural {language} that
says the same as "original" (same actions, order, times, temperatures). If not,
rewrite "marked" so it does: put an ingredient where its fixed text fits
(e.g. not "mit 3 Eier" but "3 Eier dazugeben und verrühren"), drop a
doubled amount or article. Keep the markers (each at most once, only the
given keys), never write an amount yourself, keep every number of
"original".

Respond with ONLY a JSON object (no explanation, no markdown fence):
{"steps": [{"ok": true} or {"ok": false, "marked": <corrected text>|null,
"problem": <what's wrong, a few words in {language}>}, ...]} - one per step,
same order.
"""


def _proofread(job, changed) -> None:
    """Second, cheaper AI call: reads every changed step as it will appear
    and fixes what doesn't read well. What is still doubtful afterwards gets
    change["warning"] - the suggestion is flagged for a closer look."""
    if not changed:
        return
    payload = {"steps": [{"original": c["original"], "marked": c["text"],
                          "reads_as": MARK.sub(lambda m, c=c: amount_text(c["own"][m.group(1)]), c["text"]),
                          "ingredients": [{"key": k, "text": amount_text(i)} for k, i in c["own"].items()]}
                         for c in changed]}
    answers = []
    try:
        def ask(system_prompt):
            out, usage = llm_provider.complete_tool_text(system_prompt, json.dumps(payload, ensure_ascii=False),
                                                         max_tokens=4000)
            job.token_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
            job.token_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
            return out, usage
        answer = json_answer.complete(ask, PROOFREAD_PROMPT.replace("{language}", settings.output_language))[0]
        answers = answer.get("steps") if isinstance(answer, dict) else []
    except Exception as exc:  # noqa: BLE001 - proofreading is a bonus, the local check still runs
        log.info("Proofreading the amounts failed: %s", exc)
    for n, change in enumerate(changed):
        verdict = answers[n] if isinstance(answers, list) and n < len(answers) and isinstance(answers[n], dict) else {}
        problem = None
        if verdict.get("ok") is False:
            problem = str(verdict.get("problem") or "").strip() or "reads oddly"
            fixed = verdict.get("marked")
            if isinstance(fixed, str) and fixed.strip():
                try:
                    text, seen = _checked(change["original"], fixed, change["own"], change["index"])
                except ValueError as exc:
                    log.info("Proofread fix for step %d not used: %s", change["index"] + 1, exc)
                else:
                    if seen:
                        change["text"] = text
                        change["keys"] = {k: change["own"][k].get("id") for k in seen}
                        problem = None
        issues = local_issues(change["text"], change["own"])
        if issues:
            problem = ", ".join(issues)
        if problem:
            change["warning"] = problem


def render(text, step_ingredients, key_ids, templates: bool) -> str:
    """The marked text for saving: Tandoor templates or written-out amounts."""
    position = {ing.get("id"): n for n, ing in enumerate(step_ingredients)}
    by_id = {ing.get("id"): ing for ing in step_ingredients}

    def put(match):
        row = key_ids.get(match.group(1))
        if row not in position:
            raise tandoor_client.TandoorError("The recipe's ingredients changed since the scan - rescan it.")
        if not templates:
            return amount_text(by_id[row])
        if _without_amount(by_id[row]):
            return amount_text(by_id[row])  # nothing to scale - and the template would read "0 Pfeffer"
        # Tandoor's template shows amount, unit and food - the comment is its .note
        n = position[row]
        return f"{{{{ ingredients[{n}] }}}}" + (f" ({{{{ ingredients[{n}].note }}}})" if _note(by_id[row]) else "")
    return MARK.sub(put, text)


def describe(recipe, plan) -> tuple[str, str]:
    steps = recipe.get("steps") or []
    lines = []
    for change in plan["steps"]:
        step = steps[change["index"]]
        ingredients = step.get("ingredients") or []
        after = _shown(render(change["text"], ingredients, change["keys"], templates=False), ingredients)
        lines += [f"{change['index'] + 1}. BEFORE: {_shown(step.get('instruction') or '', ingredients)}", f"   AFTER:  {after}"]
        if change.get("warning"):
            lines.append(f"   ⚠️ {change['warning']}")
    warnings = [c for c in plan["steps"] if c.get("warning")]
    flag = f"⚠️ check {len(warnings)} step(s) - " if warnings else ""
    return (f"{flag}recipe: amounts into the steps of {recipe.get('name', '')!r} ({len(plan['steps'])} step(s))",
            "\n".join(lines))


def notes_plan(recipe) -> dict:
    """No AI, for recipes done before: "{{ ingredients[n] }}" gets
    "({{ ingredients[n].note }})" where the ingredient has a comment, and an
    ingredient without an amount gets its plain name instead of the template
    (which would read "0 Pfeffer"). Text without markers - saved as it is."""
    changed = []
    for index, step in enumerate(recipe.get("steps") or []):
        missing, zero = set(_notes_missing(step)), set(_zero_templates(step))
        if not missing and not zero:
            continue
        ingredients = step.get("ingredients") or []
        done = set()

        def fix(match):
            n = int(match.group(1))
            if n in zero:
                return amount_text(ingredients[n])
            if n not in missing or n in done:
                return match.group(0)
            done.add(n)
            return f"{match.group(0)} ({{{{ ingredients[{n}].note }}}})"
        changed.append({"index": index, "text": TEMPLATE_WITH_NOTE.sub(fix, step.get("instruction") or ""), "keys": {},
                        "ids": [i.get("id") for i in step.get("ingredients") or []]})
    return {"steps": changed}


def _shown(text, step_ingredients) -> str:
    """Tandoor's templates as they read (for the preview)."""
    def put(match):
        n = int(match.group(2))
        if n >= len(step_ingredients):
            return match.group(0)
        ing = step_ingredients[n]
        if match.group(3):
            return _note(ing)
        text = amount_text({**ing, "note": ""})
        return f"0 {text}" if not ing.get("no_amount") and not ing.get("amount") else text  # as Tandoor shows it
    return re.sub(r"\{\{\s*ingredients\[((\d+))\](\.note)?\s*\}\}", put, text)


def plan_suggestion(job, recipe) -> ToolSuggestion | None:
    if any("{{" in (s.get("instruction") or "") for s in recipe.get("steps") or []):
        plan = notes_plan(recipe)
        if not plan["steps"]:
            return None
        summary, preview = describe(recipe, plan)
        return ToolSuggestion(id=uuid.uuid4().hex[:10], kind="amounts_in_steps",
                              summary=summary.replace("amounts into the steps", "fix the ingredients in the steps"),
                              preview=preview, detail={"recipe_id": recipe["id"], "plan": plan})
    try:
        plan = amounts_plan(job, recipe)
    except Exception as exc:  # noqa: BLE001
        log.warning("Amounts for recipe %s failed: %s", recipe.get("id"), exc)
        return None
    if not plan["steps"]:
        return None
    summary, preview = describe(recipe, plan)
    detail = {"recipe_id": recipe["id"], "plan": plan}
    if any(c.get("warning") for c in plan["steps"]):
        detail["flagged"] = True  # left out of "select all" - worth a look first
    return ToolSuggestion(id=uuid.uuid4().hex[:10], kind="amounts_in_steps", summary=summary, preview=preview,
                          detail=detail)


def follow_up(job, recipe) -> ToolSuggestion | None:
    """In the new-recipes run: once a recipe's structure revision was
    applied, its amounts suggestion joins the same run (the revision has to
    come first - see needs_amounts)."""
    if not needs_amounts(recipe):
        return None
    suggestion = plan_suggestion(job, recipe)
    if suggestion:
        job.suggestions.append(suggestion)
        tool_jobs.save_tool_job(job)
    return suggestion


def check_unchanged(steps, plan) -> None:
    for change in plan["steps"]:
        if change["index"] >= len(steps) or [i.get("id") for i in steps[change["index"]].get("ingredients") or []] != change["ids"]:
            raise tandoor_client.TandoorError("The recipe's steps changed since the scan - rescan it.")


# ---------- Tandoor ----------

def build_payload(recipe, plan) -> dict:
    """PATCH payload: all steps as they are (food/unit as minimal refs),
    the planned ones with the new text."""
    check_unchanged(recipe.get("steps") or [], plan)
    texts = {c["index"]: c for c in plan["steps"]}
    steps = []
    for n, old in enumerate(recipe.get("steps") or []):
        step = dict(old)
        ingredients = []
        for ing in old.get("ingredients") or []:
            ing = dict(ing)
            for field in ("food", "unit"):
                if ing.get(field) is not None:
                    ing[field] = minimal_ref(ing[field])
            ingredients.append(ing)
        step["ingredients"] = ingredients
        if n in texts:
            step["instruction"] = render(texts[n]["text"], old.get("ingredients") or [], texts[n]["keys"], templates=True)
        steps.append(step)
    return {"steps": steps}


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
            job.progress_label = "Checking which recipes lack amounts in the steps (no AI)..."
            tool_jobs.save_tool_job(job)
            candidates = recipe_scope.recipes_for(client, METRIC, needs_amounts, ignored.keys(METRIC), job)
        job.progress_total = len(candidates)
        job.cost_estimate = format_cost_estimate(len(candidates), "per_recipe_translate")
        tool_jobs.save_tool_job(job)
        suggestions = []
        for i, recipe in enumerate(candidates, 1):
            if job.cancel_requested:
                break
            job.progress_current = i
            job.progress_label = f"Amounts into the steps {i}/{len(candidates)}: {recipe.get('name', '')!r}..."
            tool_jobs.save_tool_job(job)
            suggestion = plan_suggestion(job, recipe)
            if suggestion:
                suggestions.append(suggestion)
        job.suggestions = suggestions
        job.status = "cancelled" if job.cancel_requested else "ready"
        job.progress_label = None
        tool_jobs.save_tool_job(job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Amounts scan failed for job %s", job_id)
        job.status = "error"
        job.error = str(exc)
        tool_jobs.save_tool_job(job)


def apply_plan(job, suggestion) -> ToolSuggestion:
    if suggestion.status != "pending":
        return suggestion
    try:
        with tandoor_client.get_client() as client:
            recipe_id = suggestion.detail["recipe_id"]
            resp = client.get(f"/recipe/{recipe_id}/")
            resp.raise_for_status()
            resp = client.patch(f"/recipe/{recipe_id}/", json=build_payload(resp.json(), suggestion.detail["plan"]))
            if resp.status_code not in (200, 201):
                raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status = "error"
        suggestion.error = str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion


def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        raise tandoor_client.TandoorError("Job not found.")
    suggestion = next((s for s in job.suggestions if s.id == suggestion_id), None)
    if suggestion is None:
        raise tandoor_client.TandoorError("Suggestion not found.")
    return apply_plan(job, suggestion)
