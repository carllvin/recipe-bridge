"""Picking a cookbook's recipes before the AI reads it.

A cookbook PDF or EPUB used to go to the AI whole - foreword, stories,
index and all. Before that, the import shows a list of its recipes (without
AI) to pick from; only the picked pages are sent. The list comes from, in
this order:
- the PDF's bookmarks or the EPUB's table of contents (one entry per recipe
  in many cookbooks; an entry with entries below it is a chapter and shows
  as a heading),
- otherwise the pages that look like a recipe: several lines starting with
  an amount ("200 g", "2 EL", "½ cup"), named by the page's first line.

Each entry covers its pages up to the next entry; one page more is sent so
a recipe running over the page break stays whole."""
from __future__ import annotations

import re

MIN_ENTRIES = 5       # fewer: nothing worth picking - the book is read whole
MIN_PDF_PAGES = 15    # a short PDF is read whole
MAX_PAGES_PER_ENTRY = 8  # a bookmark spanning more is a chapter, not a recipe
CHARS_PER_TOKEN = 4

# "200 g Mehl", "2 Eier", "½ TL Salz", "1-2 Zwiebeln" - an amount, then a word;
# not "1. Den Ofen …" (a numbered step) or a bare page number
_AMOUNT_LINE = re.compile(r"^\s*(?:\d+(?:[.,/]\d+)?|[½¼¾⅓⅔⅛])(?![.)\d])\s*(?:[-–]\s*\d+\s*)?[^\W\d_]")
MAX_AMOUNT_LINE = 60
MIN_AMOUNT_LINES = 3


def entries(result: dict) -> tuple[list[dict], str]:
    """([{"id", "title", "start", "end", "group"}], source) - source is
    "bookmarks" (PDF), "contents" (EPUB) or "pages" (detected)."""
    pages = result.get("pages") or []
    n = len(pages)
    toc = [e for e in result.get("toc") or [] if (e.get("title") or "").strip() and 1 <= e.get("page", 0) <= n]
    found = _from_toc(toc, n)
    if len(found) >= MIN_ENTRIES:
        return found, "contents" if result.get("kind") == "epub" else "bookmarks"
    return _from_pages(pages), "pages"


def _from_toc(toc: list[dict], n: int) -> list[dict]:
    out, group = [], None
    for i, entry in enumerate(toc):
        nxt = toc[i + 1] if i + 1 < len(toc) else None
        if nxt and nxt.get("level", 1) > entry.get("level", 1):
            group = entry["title"].strip()  # a chapter: its entries follow
            continue
        later = [e["page"] for e in toc[i + 1:] if e["page"] > entry["page"]]
        end = min((later[0] - 1) if later else n, entry["page"] + MAX_PAGES_PER_ENTRY - 1)
        out.append({"title": entry["title"].strip(), "start": entry["page"], "end": max(end, entry["page"]),
                    "group": entry.get("group") or (group if entry.get("level", 1) > 1 else None)})
    return _numbered(out)


def looks_like_recipe(text: str) -> bool:
    return sum(1 for line in (text or "").splitlines()
               if len(line.strip()) <= MAX_AMOUNT_LINE and _AMOUNT_LINE.match(line)) >= MIN_AMOUNT_LINES


def _title_of(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if 3 <= len(line) <= 80 and not _AMOUNT_LINE.match(line) and not line.isdigit():
            return line
    return ""


def _from_pages(pages: list[dict]) -> list[dict]:
    hits = [p for p in pages if looks_like_recipe(p.get("text"))]
    out = []
    for i, page in enumerate(hits):
        nxt = hits[i + 1]["page"] if i + 1 < len(hits) else page["page"] + 2
        out.append({"title": _title_of(page.get("text")) or f"#{page['page']}", "start": page["page"],
                    "end": max(page["page"], min(nxt - 1, page["page"] + 1)), "group": None})
    return _numbered(out)


def _numbered(found: list[dict]) -> list[dict]:
    return [{"id": f"e{i}", **e} for i, e in enumerate(found)]


def worth_asking(doc_type: str, result: dict, found: list[dict]) -> bool:
    if doc_type not in ("pdf", "epub") or len(found) < MIN_ENTRIES:
        return False
    return doc_type == "epub" or len(result.get("pages") or []) >= MIN_PDF_PAGES


def _key(title: str) -> str:
    return " ".join(re.findall(r"\w+", (title or "").casefold()))


def same_title(a: str, b: str) -> bool:
    """Strict: the same words (case and punctuation aside) - a list of 200
    titles has many near neighbours ("Rezept 4" / "Rezept 8")."""
    return bool(_key(a)) and _key(a) == _key(b)


def with_tokens(found: list[dict], pages: list[dict], existing_names: list[str]) -> list[dict]:
    """Each entry's text size in tokens (for the live estimate), whether its
    pages look like a recipe (ticked at first - not a foreword or index)
    and whether a recipe of that title is in the collection already
    (unticked)."""
    by_number = {p["page"]: p for p in pages}
    names = {_key(n): n for n in existing_names if _key(n)}
    out = []
    for entry in found:
        texts = [by_number.get(n, {}).get("text") or "" for n in range(entry["start"], entry["end"] + 2)]
        out.append({**entry, "tokens": sum(len(t) for t in texts) // CHARS_PER_TOKEN,
                    "pages": entry["end"] - entry["start"] + 1,
                    "recipe": any(looks_like_recipe(t) for t in texts[:entry["end"] - entry["start"] + 1]),
                    "existing": names.get(_key(entry["title"]))})
    if not any(e["recipe"] for e in out):  # nothing recognisable (e.g. a poor scan): all of them
        out = [{**e, "recipe": True} for e in out]
    return out


def estimate_basis(pages: list[dict], existing_tags) -> dict:
    """What the page needs to estimate a pick live, the way
    ai_extractor.estimate_tokens() does: tokens per page, pages per AI call
    and the instructions per call (with Claude cached after the first)."""
    from . import llm_provider
    from .ai_extractor import CHARS_PER_TOKEN as PER_TOKEN, OUTPUT_SHARE, _build_system_prompt
    prompt = len(_build_system_prompt(False, existing_tags=existing_tags)) // PER_TOKEN
    cached = llm_provider._active_provider() == "anthropic"
    return {"page_tokens": {str(p["page"]): (len(p.get("text") or "") + 30) // PER_TOKEN for p in pages},
            "pages_per_call": 12,  # picked recipes are cut between, without overlap
            "prompt_first": round(prompt * 1.25) if cached else prompt,
            "prompt_next": round(prompt * 0.1) if cached else prompt,
            "output_share": OUTPUT_SHARE}


ONLY_COMPLETES = ("[This page is only here to complete a recipe from the page before. "
                  "Do not extract a recipe that starts on this page.]\n")


def best_photos(found: list[dict], images: dict) -> dict[str, str]:
    """entry id -> the largest photo on its pages (image id) - its preview
    in the list. Entries without one get a picture of their first page."""
    by_page = {}
    for image_id, info in (images or {}).items():
        size = (info.get("width") or 0) * (info.get("height") or 0)
        page = info.get("page")
        if page is not None and size > by_page.get(page, ("", -1))[1]:
            by_page[page] = (image_id, size)
    out = {}
    for entry in found:
        candidates = [by_page[n] for n in range(entry["start"], entry["end"] + 1) if n in by_page]
        if candidates:
            out[entry["id"]] = max(candidates, key=lambda c: c[1])[0]
    return out


def units_of(found: list[dict], ids, picked: list[dict]) -> list[int]:
    """Where in the picked pages each picked recipe starts (1-based) - the
    AI calls are cut there (ai_extractor._chunk_pages units)."""
    position = {p["page"]: i for i, p in enumerate(picked, start=1)}
    return sorted({position[e["start"]] for e in found if e["id"] in ids and e["start"] in position})


def picked_pages(found: list[dict], ids, pages: list[dict]) -> tuple[list[dict], set[int]]:
    """(the pages of the picked entries - each with the page after it -,
    the page numbers a picked recipe can start on). The page after is
    marked for the AI: it may hold the next, unpicked recipe."""
    own, wanted = set(), set()
    for entry in found:
        if entry["id"] in ids:
            own.update(range(entry["start"], entry["end"] + 1))
            wanted.update(range(entry["start"], entry["end"] + 2))
    out = [p if p["page"] in own else {**p, "text": ONLY_COMPLETES + (p.get("text") or "")}
           for p in pages if p["page"] in wanted]
    return out, own
