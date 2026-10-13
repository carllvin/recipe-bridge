"""A tag with where a recipe comes from - "ottolenghi" for a cookbook by
Yotam Ottolenghi, "nytcooking" for NYT Cooking, "joshuaweissman" for his
YouTube channel - so a collection can be filtered by its source.
SOURCE_TAG=false: no such tag.

Found without AI where possible: a site's own name (og:site_name or the
recipe's publisher, else the address), a video's channel, a book's author
from its metadata (EPUB, PDF). Only a cookbook without an author in its
metadata asks the tools model, once, from its first pages."""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from urllib.parse import urlparse

from . import llm_provider
from .config import settings
from .schemas import TokenUsage

log = logging.getLogger("recipe-bridge")

MAX_LEN = 30
# a PDF's "author" is often the program or person that made the file
NOT_AN_AUTHOR = re.compile(r"adobe|microsoft|word|indesign|quark|acrobat|scan|pdf|user|admin|owner|unknown|^\W*$", re.I)
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov"}  # bbc.co.uk -> bbc


def enabled() -> bool:
    return settings.source_tag


def slug(name: str) -> str:
    """"NYT Cooking" -> "nytcooking": lower case, letters and digits only
    (umlauts stay, accents go)."""
    text = unicodedata.normalize("NFKD", (name or "").strip().lower())
    text = "".join(c for c in text if not unicodedata.combining(c) or c in "̈")
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[^\w]|_", "", text)[:MAX_LEN]


def from_author(author: str) -> str:
    """A person's surname: "Yotam Ottolenghi, Sami Tamimi" -> "ottolenghi"."""
    first = re.split(r",|;|&|\band\b|\bund\b|\bet\b", author or "", maxsplit=1)[0].strip()
    if not first or NOT_AN_AUTHOR.search(first):
        return ""
    words = [w for w in re.split(r"\s+", first) if w]
    return slug(words[-1]) if words else ""


def from_address(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and parts[-2] in SECOND_LEVEL:
        parts = parts[:-2]
    elif len(parts) >= 2:
        parts = parts[:-1]
    return slug(parts[-1]) if parts else ""


def from_page(soup, url: str, recipe: dict | None) -> str:
    """A web page: its own name - og:site_name, else the recipe's
    publisher - else the address."""
    meta = soup.find("meta", property="og:site_name") if soup is not None else None
    name = (meta.get("content") or "").strip() if meta else ""
    if not name and isinstance(recipe, dict):
        publisher = recipe.get("publisher")
        if isinstance(publisher, list):
            publisher = publisher[0] if publisher else None
        name = (publisher.get("name") if isinstance(publisher, dict) else publisher) or ""
        name = name if isinstance(name, str) else ""
    return slug(name) if slug(name) else from_address(url)


AUTHOR_PROMPT = """These are the first pages of a cookbook:

{text}

Who wrote it? Respond with ONLY a JSON object: {{"author": "<first author's full name>" or null}}.
null when no author is named."""


def guess_author(pages: list[dict]) -> tuple[str, TokenUsage]:
    """A cookbook without an author in its metadata: one small call to the
    tools model with its first pages."""
    text = "".join(f"\n\n--- page {p['page']} ---\n{(p.get('text') or '')[:1500]}" for p in pages[:4]).strip()
    if not text or not llm_provider.is_configured():
        return "", TokenUsage()
    try:
        out, usage = llm_provider.complete_tool_text(None, AUTHOR_PROMPT.format(text=text), max_tokens=60)
        match = re.search(r"\{.*\}", out or "", re.S)
        author = (json.loads(match.group(0)).get("author") if match else None) or ""
        return from_author(author if isinstance(author, str) else ""), usage or TokenUsage()
    except Exception as exc:  # noqa: BLE001 - the tag is a nice-to-have
        log.info("Cookbook author not found: %s", exc)
        return "", TokenUsage()


def add(recipes, tag: str) -> None:
    """Puts the tag on every recipe (first, once)."""
    if not tag:
        return
    for recipe in recipes:
        if tag.casefold() not in (t.casefold() for t in recipe.tags):
            recipe.tags = [tag, *recipe.tags]
