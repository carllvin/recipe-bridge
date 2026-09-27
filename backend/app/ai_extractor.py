from __future__ import annotations

import difflib
import json
import logging
import os
import re
import uuid
from typing import Callable, Optional

from . import llm_provider
from .config import settings, get_language_code
from .schemas import ExtractedRecipe, TokenUsage

log = logging.getLogger("tandoor-helper")


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


def _build_system_prompt(same_language: bool, existing_tags: Optional[list[str]] = None) -> str:
    language = settings.output_language.strip() or "English"

    unit_instruction = (
        f"IMPORTANT - Units: Convert any US/UK-style units (cups, fluid ounces, oz, "
        f"lb/pounds, °F, inches for pan sizes) to metric units (g, ml, °C, cm). Use "
        f"realistic, commonly used kitchen conversion values (e.g. 1 cup flour ≈ 120 g, "
        f"1 cup sugar ≈ 200 g, 1 cup butter ≈ 227 g, 1 cup liquid ≈ 240 ml, 1 oz ≈ 28 g, "
        f"1 lb ≈ 454 g, °F -> °C via (F-32)*5/9). Round sensibly (e.g. to the nearest "
        f"5 or 10 g/ml). Output the converted value, not the original unit. Units "
        f"already metric (g, kg, ml, l, tsp, tbsp, ...) stay unchanged."
        if settings.convert_to_metric else
        "Units: Keep the units used in the original text unchanged (no conversion)."
    )

    if same_language:
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
            f"when nothing existing fits.\n"
        )

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
{tag_reuse_instruction}
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

IMPORTANT - Missing units: if a countable ingredient has no explicit unit in the \
text (e.g. "2 onions", "3 eggs"), set "unit" to the word for "piece(s)" in \
{language} (e.g. "pcs" in English, "Stück" in German) instead of leaving it null. \
Only leave "unit" null when the ingredient genuinely has no natural unit at all \
(e.g. "salt to taste").

IMPORTANT - Side notes: if the recipe text contains important information that \
isn't part of the actual cooking steps - e.g. storage/shelf life, freezing, make- \
ahead tips, variations, allergy notes - add this as an ADDITIONAL, final step with \
the title "Note" summarizing that information. If there is no such extra \
information, do not add a note step.

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
Spring, pumpkin/mushrooms -> Autumn, mulled wine/cookies -> Winter, or an explicit \
mention in the text). If no season is clearly identifiable (e.g. an everyday dish \
available year-round, like pasta with tomato sauce), omit the season tag. Do NOT \
count this season tag towards the 5 general tags above (so up to 6 tags total).

IMPORTANT - Assign ingredients to steps: each ingredient gets a "step_index" - the \
zero-based index of the step (position in the "steps" array) where that ingredient \
is first needed/used. Read the instructions carefully (e.g. "sauté the onion" -> \
the onion belongs to that step; "season with salt and pepper" -> salt/pepper \
belong to that later step). Ingredients that belong right at the start (e.g. "prep \
the ingredients") or that can't be clearly assigned to one single step (e.g. base \
ingredients like flour in a dough processed across several steps) get step_index 0.
{custom_instruction_block}
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
                f"in {settings.output_language.strip() or 'English'} "
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
        # Fallback 2: the array as a whole is invalid (e.g. one recipe has an
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
    toc_pages: Optional[list[int]] = None,
):
    """Splits pages into chunks for the AI calls. When toc_pages (bookmark/TOC page
    numbers) are available, chunk boundaries are aligned to them - consecutive
    chapters/recipes are grouped together up to roughly pages_per_chunk, but a
    chunk never cuts a chapter in half unless a single chapter alone exceeds
    pages_per_chunk (then it falls back to the sliding-window approach for that
    chapter only). Without usable TOC data, falls back to plain fixed-size,
    overlapping windows. Either way, every chunk after the first shares an
    `overlap`-page margin with its predecessor."""
    n = len(pages)
    if n == 0:
        return

    toc_pages = sorted(set(p for p in (toc_pages or []) if 1 <= p <= n))

    if len(toc_pages) >= 2:
        ranges = _toc_chunk_ranges(n, toc_pages, pages_per_chunk, overlap)
    else:
        ranges = list(_sliding_windows(1, n, pages_per_chunk, overlap))

    for start, end in ranges:
        yield pages[start - 1:end]


def _toc_chunk_ranges(
    n: int, toc_pages: list[int], pages_per_chunk: int, overlap: int
) -> list[tuple[int, int]]:
    """Builds chapter-aligned (start, end) ranges: consecutive short chapters are
    grouped together up to ~pages_per_chunk; a single chapter longer than that is
    split on its own via the sliding window (which already overlaps its own
    sub-ranges correctly). Every NEW unit's start (a freshly flushed buffer, or
    the first sub-range of an oversized chapter) is additionally nudged
    backward - via `_start_with_overlap` - so it shares `overlap` pages with
    whatever was yielded immediately before it. This is the piece a plain
    per-range "extend every start backward" pass would get wrong: it would
    also stretch the *internal* boundaries between an oversized chapter's own
    sliding-window sub-ranges, which are already correctly overlapping and
    don't need (or want) any further adjustment."""
    boundaries = sorted(set([1] + toc_pages + [n + 1]))
    segments = [
        (boundaries[i], boundaries[i + 1] - 1)
        for i in range(len(boundaries) - 1)
        if boundaries[i] <= n
    ]

    result: list[tuple[int, int]] = []

    def append_new_unit(start: int, end: int) -> None:
        if result:
            start = _start_with_overlap(start, result[-1][1], overlap)
        result.append((start, end))

    buffer_start = None
    buffer_end = None

    for seg_start, seg_end in segments:
        seg_len = seg_end - seg_start + 1

        if seg_len > pages_per_chunk:
            # A single chapter is too long on its own - flush whatever is
            # buffered, then split this chapter with the sliding window.
            if buffer_start is not None:
                append_new_unit(buffer_start, buffer_end)
                buffer_start = None
            sub_ranges = list(_sliding_windows(seg_start, seg_end, pages_per_chunk, overlap))
            for i, (sub_start, sub_end) in enumerate(sub_ranges):
                if i == 0:
                    append_new_unit(sub_start, sub_end)
                else:
                    result.append((sub_start, sub_end))  # already correctly overlapping internally
            continue

        if buffer_start is None:
            buffer_start, buffer_end = seg_start, seg_end
        elif (seg_end - buffer_start + 1) <= pages_per_chunk:
            buffer_end = seg_end
        else:
            append_new_unit(buffer_start, buffer_end)
            buffer_start, buffer_end = seg_start, seg_end

    if buffer_start is not None:
        append_new_unit(buffer_start, buffer_end)

    return result


def _start_with_overlap(start: int, prev_end: int, overlap: int) -> int:
    """Nudges `start` backward just enough to share `overlap` pages with a
    previous range ending at `prev_end`, but never extends it forward (if
    there's already at least that much overlap, or even a full containment,
    `start` is left untouched) and never below page 1."""
    target = max(1, prev_end - overlap + 1)
    return min(start, target)


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


def extract_recipes_from_pages(
    pages: list[dict],
    on_progress: Optional[ProgressCallback] = None,
    toc_pages: Optional[list[int]] = None,
    existing_tags: Optional[list[str]] = None,
) -> tuple[list[ExtractedRecipe], TokenUsage]:
    if not llm_provider.is_configured():
        raise RuntimeError(llm_provider.missing_key_hint())

    # Once per job, check whether the cookbook is already in the target language -
    # if so, the AI doesn't need to "translate", just structure (see _build_system_prompt).
    source_lang = _detect_source_language(pages)
    target_lang = get_language_code(settings.output_language)
    same_language = bool(source_lang and target_lang and source_lang == target_lang)
    system_prompt = _build_system_prompt(same_language, existing_tags=existing_tags)

    chunks = list(_chunk_pages(pages, toc_pages=toc_pages))
    total_chunks = len(chunks) or 1
    raw_recipes: list[dict] = []
    total_usage = TokenUsage()

    log.info(
        "Extraction starting: %d page(s) split into %d chunk(s) (toc_pages=%s)",
        len(pages), total_chunks, toc_pages or "none",
    )

    for idx, chunk in enumerate(chunks, start=1):
        chunk_text = "".join(
            f"\n\n===== PAGE {p['page']} =====\n{p['text']}" for p in chunk
        )
        page_start, page_end = chunk[0]["page"], chunk[-1]["page"]

        if not chunk_text.strip():
            log.warning("Chunk %d/%d (pages %d-%d) has no extractable text - skipping", idx, total_chunks, page_start, page_end)
            if on_progress:
                on_progress(idx, total_chunks, page_start, page_end)
            continue

        text_out, usage = llm_provider.complete_text(system_prompt, chunk_text, max_tokens=8000)
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


def _dedupe_recipes(raw_recipes: list[dict]) -> list[ExtractedRecipe]:
    """Removes duplicates caused by overlapping chunks (similar title + overlapping page range)."""
    results: list[ExtractedRecipe] = []

    def overlaps(a_start, a_end, b_start, b_end) -> bool:
        return a_start <= b_end and b_start <= a_end

    for item in raw_recipes:
        try:
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

        is_duplicate = False
        for existing in results:
            same_title = _titles_likely_same(existing.title, recipe.title)
            pages_overlap = overlaps(
                existing.source_page_start, existing.source_page_end,
                recipe.source_page_start, recipe.source_page_end,
            )
            if same_title and pages_overlap:
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
