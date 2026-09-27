"""Plain text as an import source: pasted recipe text, Markdown files and
.txt files. Returns the same shape as the other processors (pdf_processor
...), so the normal extraction, review and import follow.

A .txt file is either a list of recipe links or recipe text - see
looks_like_link_list()."""
from __future__ import annotations

import re

from .url_processor import links_from_text

MAX_PAGE_CHARS = 6000  # long texts are split into "pages" at paragraph breaks
MAX_TEXT_CHARS = 400_000


def split_pages(text: str, max_chars: int = MAX_PAGE_CHARS) -> list[dict]:
    """Splits text into pages of at most ~max_chars, preferably at blank
    lines, so a long text (several recipes) runs through the same chunked
    extraction as a book."""
    text = (text or "").replace("\r\n", "\n")[:MAX_TEXT_CHARS].strip()
    pages, current = [], ""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        while len(block) > max_chars:  # one huge paragraph: hard split
            if current:
                pages.append(current)
                current = ""
            pages.append(block[:max_chars])
            block = block[max_chars:]
        if current and len(current) + len(block) + 2 > max_chars:
            pages.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        pages.append(current)
    return [{"page": i, "text": t} for i, t in enumerate(pages, 1)]


def process_text(text: str) -> dict:
    pages = split_pages(text)
    if not pages:
        raise ValueError("The text is empty.")
    return {"pages": pages, "images": {}, "page_count": len(pages), "metadata_title": "", "toc_pages": []}


def looks_like_link_list(text: str) -> bool:
    """True for a file that is mostly links (one per line, maybe with short
    labels), False for recipe text that merely mentions a link."""
    links = links_from_text(text)
    if not links:
        return False
    rest = re.sub(r"https?://\S+", "", text or "")
    rest = re.sub(r"\s+", " ", rest).strip()
    return len(rest) <= 60 * len(links) + 80


def title_from_text(text: str, limit: int = 40) -> str:
    """A short name for the import list: the first non-empty line."""
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return "Text"
