from __future__ import annotations

import difflib
import json
import logging
import os
import re
import uuid
from typing import Callable, Optional

from . import json_answer, llm_provider
from .config import settings, get_language_code
from .schemas import ExtractedRecipe, TokenUsage

log = logging.getLogger("recipe-bridge")


def _detect_source_language(pages: list[dict]) -> Optional[str]:
    """Detects the language of the first few pages (ISO 639-1) without calling the AI API.
    Returns None if that isn't reliably possible (e.g. too little text)."""
    sample = " ".join(p["text"] for p in pages[:3])[:3000].strip()
    if len(sample) < 50:
        return None
    try:
        from langdetect import DetectorFactory, detect, LangDetectException
        DetectorFactory.seed = 0  # same answer every time
        try:
            return detect(sample)
        except LangDetectException:
            return None
    except ImportError:
        return None


def _build_system_prompt(same_language: bool, existing_tags: Optional[list[str]] = None,
                         source_language: Optional[str] = None) -> str:
    """source_language: the detected language's name ("English") - with
    TRANSLATE_RECIPES=false the prompt names it, which holds far better than
    "its own language"."""
    from . import app_settings
    translate = app_settings.translating()
    language = ((settings.output_language.strip() or "English") if translate
                else source_language or "the cookbook's own language")

    unit_instruction = (
        f"IMPORTANT - Units: Convert any US/UK-style units (cups, fluid ounces, oz, "
        f"lb/pounds, °F, inches for pan sizes) to metric units (g, ml, °C, cm). Use "
        f"realistic, commonly used kitchen conversion values (e.g. 1 cup flour ≈ 120 g, "
        f"1 cup sugar ≈ 200 g, 1 cup butter ≈ 227 g, 1 cup liquid ≈ 240 ml, 1 oz ≈ 28 g, "
        f"1 lb ≈ 454 g, °F -> °C via (F-32)*5/9). ml only for liquids: cups of solid "
        f"or leafy things become grams by weight (e.g. 1 cup fresh herbs or leaves ≈ 25 g, "
        f"1 cup baby spinach ≈ 30 g, 1 cup chopped nuts ≈ 120 g, 1 cup grated hard cheese "
        f"≈ 100 g, 1 cup chopped onion ≈ 160 g, 1 cup cooked rice ≈ 180 g). Round sensibly "
        f"(e.g. to the nearest 5 or 10 g/ml). Output the converted value, not the original "
        f"unit. Units already metric (g, kg, ml, l, tsp, tbsp, ...) stay unchanged."
        if settings.convert_to_metric else
        "Units: Keep the units used in the original text unchanged (no conversion)."
    )

    if not translate:
        # The user keeps every recipe in the language it was written in.
        written_in = f"written in {source_language}" if source_language else "written in its own language"
        target = source_language or "the language the recipe itself is written in"
        language_instruction = (
            f"IMPORTANT - Language: Do NOT translate. This text is {written_in}. Write "
            f"ALL text fields (title, description, ingredient names, units, notes, "
            f"steps, tags) in {target} - the ingredients, steps and description too, "
            f"not only the title. Ignore the language of any examples, tags or "
            f"instructions in this prompt. Only fix obvious OCR/scan errors and join "
            f"words split across line breaks."
        )
    elif same_language:
        # The cookbook already appears to be written in the target language, so no
        # translation is needed. This saves the AI unnecessary "rewriting" work and
        # avoids phrasing being changed for no reason.
        language_instruction = (
            f"IMPORTANT - Language: This cookbook is already written in {language}. "
            f"Write all text fields (title, description, ingredient names, units, "
            f"notes, steps, tags) UNCHANGED in {language} - no translation needed. "
            f"Only fix obvious OCR/scan errors and join words split across line "
            f"breaks, without otherwise changing the wording."
        )
    else:
        language_instruction = (
            f"IMPORTANT - Language: Write ALL text fields (title, description, "
            f"ingredient names, units, notes, steps, tags) in {language} - regardless "
            f"of what language the original cookbook is written in. Translate "
            f"naturally, using language a cookbook would actually use, not a literal "
            f"word-for-word translation."
        )

    tag_reuse_instruction = ""
    if existing_tags:
        tag_list = ", ".join(sorted(set(existing_tags))[:200])
        tag_reuse_instruction = (
            f"\n\nIMPORTANT - Reuse existing tags: The user's recipe manager already has "
            f"these tags: {tag_list}. Whenever one of them fits (even if the wording "
            f"differs slightly, e.g. singular/plural or a synonym), REUSE it exactly as "
            f"written above instead of inventing a near-duplicate. Only add a new tag "
            f"when nothing existing fits."
            + ("" if translate else " Only reuse a tag that is in the recipe's own language - never translate a tag.")
            + "\n"
        )

    # Diet / allergen tags right away, so an import through this app needs no
    # extra tag suggestions afterwards (see tools_new_recipes).
    from .tools_tags import diet_tags  # late import: tools_tags pulls in the Tandoor client
    diet = diet_tags(existing_tags or [])
    diet_instruction = (
        f"\n\nIMPORTANT - Diet tags: also add, when they apply, these tags exactly as "
        f"written: {', '.join(repr(d) for d in diet)}. Judge them by the ingredient list: "
        f"{diet[0]!r} only without meat and fish, {diet[1]!r} only without any animal "
        f"product (then add {diet[0]!r} too). The free-from tags ({', '.join(repr(d) for d in diet[2:])}) "
        f"only when the complete ingredient list clearly contains none of it (e.g. no "
        f"wheat flour, pasta, bread, soy sauce for gluten; no milk, butter, cream, cheese "
        f"for lactose; no nuts or nut products for nut-free) - when in doubt, leave it "
        f"out. Diet tags don't count towards the 5 general tags."
    )

    from . import step_amounts
    amounts_instruction = f"\n{step_amounts.PROMPT}" if step_amounts.wanted() else ""

    custom_instruction_block = ""
    if settings.custom_instructions.strip():
        custom_instruction_block = (
            f"\n\nADDITIONAL USER INSTRUCTIONS (these take precedence over any "
            f"conflicting guidance above):\n{settings.custom_instructions.strip()}\n"
        )

    return f"""You are an expert at digitizing cookbooks.
You will receive the text of several consecutive PDF pages from a cookbook; each \
page starts with a marker "===== PAGE n =====". The cookbook may be written in \
any language.

Your task: identify every individual recipe in this excerpt and extract it in a \
structured form. Ignore the table of contents, index, foreword, ads, and purely \
decorative/chapter pages with no actual recipe on them.

{language_instruction}

{unit_instruction}
{tag_reuse_instruction}{diet_instruction}
A recipe may span multiple pages, or several recipes may appear on one page. Use \
the page markers to determine source_page_start and source_page_end correctly \
(the actual page number, not the position within the text).

If a recipe begins or ends at the edge of this excerpt and therefore looks \
incomplete, still extract as much as you can - the system processes pages in \
overlapping sections, and incomplete duplicates are merged afterwards.

IMPORTANT - Keep ingredient names simple: the "name" field should be a clean, \
generic ingredient name only (e.g. "potatoes", "onion", "butter") - never the name \
plus a preparation note. Preparation details such as "peeled", "diced", "melted", \
"at room temperature", "finely chopped" belong in the ingredient's "note" field \
instead, not in "name". This keeps ingredient names reusable and consistent \
between recipes (so "potatoes, peeled" and "potatoes, diced" both become the same \
ingredient "potatoes" with different notes).

IMPORTANT - Alternatives: when the text offers a choice ("butter or margarine", \
"lemon, lime or orange juice"), only the first is the ingredient ("name": \
"butter"); the others go into its "note", e.g. "or margarine" (in {language}).
Every word is written out in full: a shortened compound like "Romano- oder \
Wachsbohnen" becomes "Romanobohnen" with the note "oder Wachsbohnen" - a name \
never ends with a hyphen.

IMPORTANT - Missing units: if a countable ingredient has no explicit unit in the \
text (e.g. "2 onions", "3 eggs"), set "unit" to the word for "piece(s)" in \
{language} (e.g. "pcs" in English, "Stück" in German) instead of leaving it null. \
Only leave "unit" null when the ingredient genuinely has no natural unit at all \
(e.g. "salt to taste").
When the portion is part of the name, it becomes the unit and the name stays the \
ingredient: "3 garlic cloves" / "3 Knoblauchzehen" -> amount 3, unit "clove(s)" / \
"Zehe(n)", name "garlic" / "Knoblauch" - never "3 pcs garlic" (three whole bulbs). \
The same for a stalk of celery, a sprig of thyme, a slice of bread, a bunch of \
parsley, a can of tomatoes.

IMPORTANT - Side notes: if the recipe text contains important information that \
isn't part of the actual cooking steps - e.g. storage/shelf life, freezing, make- \
ahead tips, variations, allergy notes - add this as an ADDITIONAL, final step with \
the title "Note" summarizing that information. If there is no such extra \
information, do not add a note step.

IMPORTANT - The cook's own notes are never dropped: handwritten or added notes \
(marked [Note: ... | belongs to: ...] in a transcription, or remarks like "less \
sugar!", "+ cinnamon", "bake 10 min longer", "very good", "from Aunt Erna") must \
all appear in the recipe, in their own words (translated if needed). A note about \
one ingredient goes into that ingredient's "note"; a note about one step is added \
to that step's instruction in brackets, e.g. "(Note: 10 min longer)"; everything \
else goes into the final "Note" step. Never apply a note silently by changing an \
amount or time - write it down. A [Corrected: new (was old)] amount is the valid \
one: use the new value.

IMPORTANT - Length limits: "title" must be at most 128 characters and \
"description" at most 512 characters (these are hard limits in the recipe \
manager this will be imported into - anything longer gets cut off with no \
regard for where a sentence or word ends). If the source text's title or intro \
is longer than that, shorten it yourself, keeping the meaning - don't just \
write a long value and let it run over.

Times (prep_time_minutes, cook_time_minutes, total_time_minutes) are always \
whole minutes; use null if not stated.

tags: short, general keywords in {language} (e.g. "vegetarian", "dessert", \
"quick"), at most 5, only when derivable from the text or very obvious.

IMPORTANT - Season tag: additionally add EXACTLY ONE season tag to the tags - one \
of "Spring", "Summer", "Autumn", or "Winter" (translated into {language}) - if the \
recipe clearly fits a season (e.g. main ingredients like asparagus/strawberries -> \
Spring, pumpkin/mushrooms -> Autumn, mulled wine/cookies -> Winter), it's a dish of a \
holiday (Christmas baking like stollen, panettone or gingerbread -> Winter, Easter \
bread -> Spring - also when a tag like "Christmas" is already there), or an explicit \
mention in the text. If no season is clearly identifiable (e.g. an everyday dish \
available year-round, like pasta with tomato sauce), omit the season tag. Do NOT \
count this season tag towards the 5 general tags above (so up to 6 tags total).

IMPORTANT - Assign ingredients to steps: each ingredient gets a "step_index" - the \
zero-based index of the step (position in the "steps" array) where that ingredient \
is first needed/used. Read the instructions carefully (e.g. "sauté the onion" -> \
the onion belongs to that step; "season with salt and pepper" -> salt/pepper \
belong to that later step). Ingredients that belong right at the start (e.g. "prep \
the ingredients") or that can't be clearly assigned to one single step (e.g. base \
ingredients like flour in a dough processed across several steps) get step_index 0.
{amounts_instruction}{custom_instruction_block}
IMPORTANT - Valid JSON only: the output MUST be syntactically valid JSON. Any \
double-quote character that appears inside a text value (a quoted word, a \
typographic quotation mark copied from the source, an inch/size mark like 9") \
MUST be escaped as \\" so it does not terminate the string early - or, simpler, \
just rephrase to avoid embedding quotation marks in text values at all. A single \
unescaped quote breaks the entire response, so when in doubt, leave it out.

Respond ONLY with a JSON array (no explanations, no markdown code fence), each \
element following this schema:

{{
  "title": string,
  "description": string | null,
  "servings": integer | null,
  "prep_time_minutes": integer | null,
  "cook_time_minutes": integer | null,
  "total_time_minutes": integer | null,
  "tags": string[],
  "ingredients": [
    {{"amount": number | null, "unit": string | null, "name": string, "note": string | null, "group": string | null, "step_index": integer}}
  ],
  "steps": [
    {{"title": string | null, "instruction": string, "time_minutes": integer | null}}
  ],
  "source_page_start": integer,
  "source_page_end": integer
}}

If this excerpt contains no recipe at all, respond with [].
"""


def app_settings_translating() -> bool:
    from . import app_settings
    return app_settings.translating()


def _title_language() -> str:
    from . import app_settings
    return (settings.output_language.strip() or "English") if app_settings.translating() else "the cookbook's own language"


def guess_cookbook_title(pages: list[dict], filename: str, metadata_title: str = "") -> tuple[str, TokenUsage]:
    """Derives a cookbook-name suggestion, preferring (in order) the PDF's own
    title metadata, an AI guess from the first pages, and finally the filename."""
    fallback = (
        os.path.splitext(filename)[0].replace("_", " ").replace("-", " ").strip().title()
        or "Imported Cookbook"
    )
    usage = TokenUsage()

    if metadata_title and len(metadata_title) > 2:
        return metadata_title, usage

    if not llm_provider.is_configured():
        return fallback, usage

    intro_text = "".join(
        f"\n\n===== PAGE {p['page']} =====\n{p['text']}" for p in pages[:3]
    ).strip()
    if not intro_text:
        return fallback, usage

    try:
        text_out, usage = llm_provider.complete_text(
            system_prompt=None,
            user_content=(
                f"These are the first pages of a scanned cookbook PDF "
                f"(filename: \"{filename}\"):\n\n{intro_text}\n\n"
                f"Respond with ONLY the likely title of this cookbook as plain text "
                f"in {_title_language()} "
                f"(no quotation marks, no explanation, no subtitle). "
                f"If no title is recognizable, derive a short, sensible name from "
                f"the filename instead."
            ),
            max_tokens=60,
        )
        text_out = text_out.strip().strip('"').strip("'").strip()
        return (text_out or fallback), usage
    except Exception:
        return fallback, usage


def _extract_json_array(raw: str) -> list:
    raw = raw.strip()
    # In case the AI adds code-fence markers despite being told not to, strip them
    raw = re.sub(r"^```(json)?", "", raw.strip())
    raw = re.sub(r"```$", "", raw.strip())
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fallback 1: look for the largest [...] segment in the text
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        # Fallback 2: repair the usual slips (unescaped quote in a text,
        # line break inside a string, trailing comma).
        try:
            repaired = json_answer.parse(raw)
            if isinstance(repaired, list):
                return repaired
        except json.JSONDecodeError:
            pass
        # Fallback 3: the array as a whole is invalid (e.g. one recipe has an
        # unescaped quote in a text field), but individual recipe objects
        # within it may still each be valid JSON on their own. Salvage those
        # instead of discarding every recipe in the chunk over one bad object.
        salvaged = _salvage_json_objects(raw)
        if salvaged:
            log.warning(
                "Recovered %d recipe(s) from an otherwise invalid JSON response "
                "by parsing individual objects (some recipes in this chunk may "
                "still have been lost - see the raw response logged above).",
                len(salvaged),
            )
            return salvaged
        raise


def _salvage_json_objects(raw: str) -> list[dict]:
    """Scans raw text for top-level {...} objects (matching braces, aware of
    strings/escapes) and returns the ones that parse as valid JSON on their
    own. Used as a last resort when the overall array is malformed."""
    objects: list[dict] = []
    depth = 0
    start = None
    in_string = False
    escape = False

    for i, ch in enumerate(raw):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = raw[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        objects.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None

    return objects


def _chunk_pages(
    pages: list[dict],
    pages_per_chunk: int = 12,
    overlap: int = 2,
    units: Optional[list[int]] = None,
):
    """Splits pages into chunks for the AI calls: fixed-size windows, each
    sharing `overlap` pages with the one before, so a recipe cut at one
    window's edge is whole in the next (the second reading is merged,
    _same_recipe). units: picked recipes (recipe_picker.py) - the positions
    where each starts; each is whole already (with the page after it), so
    they're grouped up to pages_per_chunk without overlap and never cut - no
    recipe is read twice."""
    n = len(pages)
    if n == 0:
        return

    if units:
        ranges = _unit_ranges(n, units, pages_per_chunk)
    else:
        ranges = list(_sliding_windows(1, n, pages_per_chunk, overlap))

    for start, end in ranges:
        yield pages[start - 1:end]


def _unit_ranges(n: int, units: list[int], pages_per_chunk: int) -> list[tuple[int, int]]:
    bounds = sorted(set([1] + [u for u in units if 1 <= u <= n] + [n + 1]))
    ranges: list[tuple[int, int]] = []
    for start, end in ((bounds[i], bounds[i + 1] - 1) for i in range(len(bounds) - 1)):
        if ranges and end - ranges[-1][0] + 1 <= pages_per_chunk:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    return ranges


def _sliding_windows(start: int, end: int, size: int, overlap: int):
    """Yields (start, end) page ranges (1-indexed, inclusive) covering [start, end]
    in overlapping windows of up to `size` pages."""
    i = start
    while i <= end:
        window_end = min(i + size - 1, end)
        yield i, window_end
        if window_end >= end:
            break
        i = window_end - overlap + 1


ProgressCallback = Callable[[int, int, int, int], None]  # (chunk_idx, total_chunks, page_start, page_end)


CHARS_PER_TOKEN = 4      # rough, for the estimate behind the "large import" question
OUTPUT_SHARE = 0.6       # the recipes' JSON is about this share of the text it comes from
MAX_OUTPUT_TOKENS = 8000  # per chunk (complete_text below)


def estimate_tokens(pages: list[dict], existing_tags: Optional[list[str]] = None,
                    units: Optional[list[int]] = None) -> dict:
    """{"chunks", "input", "output", "total"} - roughly what the extraction
    of these pages will use, without asking the AI: the chunks' text (with
    the overlapping pages counted twice) plus the instructions per chunk
    (with Claude cached after the first), and the answers as a share of the
    text."""
    prompt = len(_build_system_prompt(False, existing_tags=existing_tags)) // CHARS_PER_TOKEN
    chunks = [c for c in _chunk_pages(pages, units=units)
              if any((p.get("text") or "").strip() for p in c)]
    texts = [sum(len(p.get("text") or "") + 30 for p in c) // CHARS_PER_TOKEN for c in chunks]
    if len(chunks) > 1 and llm_provider._active_provider() == "anthropic":
        # cached after the first chunk (complete_text cache=True): written once at 1.25x, then read at a tenth
        prompt_total = round(prompt * 1.25) + round(prompt * 0.1) * (len(chunks) - 1)
    else:
        prompt_total = prompt * len(chunks)
    tokens_in = sum(texts) + prompt_total
    tokens_out = sum(min(MAX_OUTPUT_TOKENS, int(t * OUTPUT_SHARE)) for t in texts)
    return {"chunks": len(chunks), "input": tokens_in, "output": tokens_out, "total": tokens_in + tokens_out}


def _system_prompt_for(pages: list[dict], existing_tags: Optional[list[str]] = None) -> str:
    """The extraction's instructions for these pages - whether they are in
    the target language already decides between translating and keeping
    the wording (see _build_system_prompt)."""
    source_lang = _detect_source_language(pages)
    target_lang = get_language_code(settings.output_language)
    same_language = bool(source_lang and target_lang and source_lang == target_lang)
    from .config import LANGUAGE_NAMES
    system_prompt = _build_system_prompt(same_language, existing_tags=existing_tags,
                                         source_language=LANGUAGE_NAMES.get(source_lang or ""))
    log.info("Extraction language: translate=%s, detected=%s, instruction: %s", app_settings_translating(),
             source_lang, next((line for line in system_prompt.splitlines() if "IMPORTANT - Language" in line), "-"))
    return system_prompt


def _page_text(pages: list[dict]) -> str:
    return "".join(f"\n\n===== PAGE {p['page']} =====\n{p['text']}" for p in pages)


def extract_each_as_batch(page_sets: list[list[dict]], existing_tags: Optional[list[str]] = None,
                          on_wait=None, on_submit=None,
                          batch_ref: Optional[dict] = None) -> list[tuple[list[ExtractedRecipe], TokenUsage]]:
    """A link list as one batch: every page set (one web page each) is its
    own request with its own instructions, all at half price. on_submit /
    batch_ref: see extract_recipes_from_pages. Raises when the batch fails
    - the caller then reads them one by one."""
    if batch_ref is not None:  # sent before a restart - only the answers are missing
        answers = llm_provider.collect_batch(batch_ref, on_wait)
    else:
        systems = [_system_prompt_for(pages, existing_tags) for pages in page_sets]
        answers = llm_provider.complete_batch(systems, [_page_text(pages) for pages in page_sets],
                                              max_tokens=8000, on_wait=on_wait, on_submit=on_submit)
    out = []
    for (text_out, usage) in answers:
        try:
            parsed = _extract_json_array(text_out)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not parse a batch answer as JSON (%s)", exc)
            parsed = []
        out.append((_dedupe_recipes(parsed), usage))
    return out


def extract_recipes_from_pages(
    pages: list[dict],
    on_progress: Optional[ProgressCallback] = None,
    existing_tags: Optional[list[str]] = None,
    batch: bool = False,
    on_wait=None,
    on_submit=None,
    batch_ref: Optional[dict] = None,
    units: Optional[list[int]] = None,
) -> tuple[list[ExtractedRecipe], TokenUsage]:
    """batch=True (the import page's checkbox, WATCH_BATCH): all chunks as
    one batch at half price - on_wait(done, total) while it runs; falls
    back to one call per chunk when the provider has no batches or the
    batch fails. on_submit(ref) once the batch is sent (the caller keeps it
    for a restart); batch_ref: a batch sent before a restart - its answers
    are collected instead of sending again."""
    if not llm_provider.is_configured():
        raise RuntimeError(llm_provider.missing_key_hint())
    system_prompt = _system_prompt_for(pages, existing_tags)

    chunks = list(_chunk_pages(pages, units=units))
    total_chunks = len(chunks) or 1
    raw_recipes: list[dict] = []
    total_usage = TokenUsage()

    log.info(
        "Extraction starting: %d page(s) split into %d chunk(s)%s",
        len(pages), total_chunks, " at the picked recipes" if units else "",
    )

    texts = [_page_text(chunk) for chunk in chunks]
    answers: dict[int, tuple[str, TokenUsage]] = {}
    wanted = [n for n, t in enumerate(texts) if t.strip()]
    if (batch or batch_ref) and llm_provider.batch_supported() and wanted:
        try:
            log.info("Extraction as one AI batch: %d chunk(s), half price, up to 24 h", len(wanted))
            results = (llm_provider.collect_batch(batch_ref, on_wait) if batch_ref is not None else
                       llm_provider.complete_batch(system_prompt, [texts[n] for n in wanted], max_tokens=8000,
                                                   on_wait=on_wait, on_submit=on_submit))
            answers = dict(zip(wanted, results))
        except Exception as exc:  # noqa: BLE001 - one by one instead
            log.warning("AI batch failed (%s) - asking chunk by chunk", exc)

    for idx, chunk in enumerate(chunks, start=1):
        chunk_text = texts[idx - 1]
        page_start, page_end = chunk[0]["page"], chunk[-1]["page"]

        if not chunk_text.strip():
            log.warning("Chunk %d/%d (pages %d-%d) has no extractable text - skipping", idx, total_chunks, page_start, page_end)
            if on_progress:
                on_progress(idx, total_chunks, page_start, page_end)
            continue

        if idx - 1 in answers:
            text_out, usage = answers[idx - 1]
        else:
            text_out, usage = llm_provider.complete_text(system_prompt, chunk_text, max_tokens=8000,
                                                         cache=total_chunks > 1)
        total_usage.add(usage)
        try:
            parsed = _extract_json_array(text_out)
        except Exception as exc:
            log.warning(
                "Chunk %d/%d (pages %d-%d): could not parse AI response as JSON (%s). "
                "First 500 chars of the raw response:\n%s",
                idx, total_chunks, page_start, page_end, exc, text_out[:500],
            )
            parsed = []

        if not parsed:
            log.info("Chunk %d/%d (pages %d-%d): AI found no recipes in this chunk", idx, total_chunks, page_start, page_end)
        else:
            log.info(
                "Chunk %d/%d (pages %d-%d): found %d recipe(s): %s",
                idx, total_chunks, page_start, page_end, len(parsed),
                ", ".join(r.get("title", "?") for r in parsed if isinstance(r, dict)),
            )

        raw_recipes.extend(parsed)

        if on_progress:
            on_progress(idx, total_chunks, page_start, page_end)

    return _dedupe_recipes(raw_recipes), total_usage


def _titles_likely_same(a: str, b: str, threshold: float = 0.78) -> bool:
    """True for an exact match, or a fuzzy match above `threshold`. Chunk overlap
    can cause the AI to phrase the same recipe's title slightly differently
    between two overlapping calls (e.g. minor wording/translation drift), so an
    exact string match alone misses those and lets near-duplicates through."""
    a_norm, b_norm = a.strip().lower(), b.strip().lower()
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm:
        return True
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio() >= threshold


PAGE_SLIP = 3      # a chunk may name a recipe's pages slightly off (printed vs. PDF page numbers)
NEAR_PAGES = 12    # the same recipe read twice lies within one chunk's reach
SAME_METHOD = 0.6  # share of the shorter method found in the other one


def _ingredient_names(recipe: ExtractedRecipe) -> set[str]:
    return {re.sub(r"\s+", " ", (i.name or "").strip().casefold()) for i in recipe.ingredients if (i.name or "").strip()}


def _contained(small: set, large: set) -> float:
    return len(small & large) / min(len(small), len(large)) if small and large else 0.0


def _method_overlap(a: ExtractedRecipe, b: ExtractedRecipe) -> float:
    """How much of the shorter method text is found in the other one - a
    partial reading (cut at a chunk edge) is contained in the full one, a
    variant ("Apfelpfannkuchen") is written differently."""
    texts = sorted((" ".join(s.instruction or "" for s in r.steps).casefold()[:2000] for r in (a, b)), key=len)
    short, long = texts
    if len(short) < 40:
        return 0.0
    matcher = difflib.SequenceMatcher(None, short, long, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks()) / len(short)


def _same_recipe(a: ExtractedRecipe, b: ExtractedRecipe) -> bool:
    """Two readings of one recipe from overlapping chunks. The titles may
    drift a little ("mit Feta" / "mit Schafskäse") and so may the pages a
    chunk names (printed page numbers); the ingredients and the method tell
    a second reading from a neighbouring variant."""
    gap = max(a.source_page_start, b.source_page_start) - min(a.source_page_end, b.source_page_end)
    if gap > NEAR_PAGES:
        return False
    names_a, names_b = _ingredient_names(a), _ingredient_names(b)
    if _titles_likely_same(a.title, b.title) and gap <= PAGE_SLIP:
        # the same title close by - unless the ingredients clearly differ (two "Dressing"s);
        # on pages that don't overlap the method has to match too ("Pfannkuchen" / "Apfelpfannkuchen")
        if names_a and names_b and _contained(names_a, names_b) < 0.5:
            return False
        return gap <= 0 or _method_overlap(a, b) >= SAME_METHOD
    if min(len(names_a), len(names_b)) < 2 or _contained(names_a, names_b) < 0.7:
        return False
    return _method_overlap(a, b) >= SAME_METHOD


def _dedupe_recipes(raw_recipes: list[dict]) -> list[ExtractedRecipe]:
    """Removes duplicates caused by overlapping chunks (_same_recipe), keeping
    the reading with more detail."""
    results: list[ExtractedRecipe] = []

    from . import step_amounts
    for item in raw_recipes:
        try:
            if isinstance(item, dict):
                step_amounts.from_extraction(item)
                _first_alternative(item.get("ingredients"))
                from .tools_tags import with_holiday_season  # late import: pulls in the Tandoor client
                item["tags"] = with_holiday_season([t for t in item.get("tags") or [] if isinstance(t, str)],
                                                   item.get("title") or "")
            recipe = ExtractedRecipe(
                id=uuid.uuid4().hex[:10],
                title=item.get("title") or "Untitled recipe",
                description=item.get("description"),
                servings=item.get("servings"),
                prep_time_minutes=item.get("prep_time_minutes"),
                cook_time_minutes=item.get("cook_time_minutes"),
                total_time_minutes=item.get("total_time_minutes"),
                tags=item.get("tags") or [],
                ingredients=item.get("ingredients") or [],
                steps=item.get("steps") or [],
                source_page_start=item.get("source_page_start") or 1,
                source_page_end=item.get("source_page_end") or item.get("source_page_start") or 1,
            )
        except Exception:
            continue
        if not recipe.ingredients and not recipe.steps:
            # only a title - e.g. made up from a bare link - is no recipe
            log.info("Skipping %r: no ingredients and no steps", recipe.title)
            continue

        is_duplicate = False
        for existing in results:
            if _same_recipe(existing, recipe):
                # Keep whichever version has more detail (more ingredients/steps),
                # and prefer the wider page range either way (a chunk that only
                # saw part of the recipe reports a narrower range than one that
                # saw all of it).
                more_detail = len(recipe.ingredients) + len(recipe.steps) > len(existing.ingredients) + len(existing.steps)
                if more_detail:
                    idx = results.index(existing)
                    recipe.source_page_start = min(existing.source_page_start, recipe.source_page_start)
                    recipe.source_page_end = max(existing.source_page_end, recipe.source_page_end)
                    results[idx] = recipe
                else:
                    existing.source_page_start = min(existing.source_page_start, recipe.source_page_start)
                    existing.source_page_end = max(existing.source_page_end, recipe.source_page_end)
                is_duplicate = True
                break

        if not is_duplicate:
            results.append(recipe)

    # Sort by page number so the UI list follows the book's order
    results.sort(key=lambda r: (r.source_page_start, r.source_page_end))
    return results


def _first_alternative(ingredients) -> None:
    """"Butter oder Margarine" -> "Butter", note "oder Margarine" - for
    what slips past the prompt's rule."""
    from .prep_notes import _compound_end, complete_compounds, split_alternatives, with_note
    for ing in ingredients or []:
        if not isinstance(ing, dict) or not isinstance(ing.get("name"), str):
            continue
        if ing["name"].rstrip().endswith("-"):  # "frische Romano-" + note "oder Wachs-, grüne Bohnen"
            rest = ing.get("note") or ""
            end = _compound_end(rest)
            if end:
                ing["name"] = ing["name"].rstrip()[:-1] + end
                ing["note"] = complete_compounds(rest, end)
            else:  # can't tell: keep it readable as one
                ing["name"], ing["note"] = f"{ing['name'].rstrip()} {rest}".strip(), None
            continue
        name, note = split_alternatives(ing["name"])
        if note:
            ing["name"] = name
            ing["note"] = with_note(ing.get("note"), note)
