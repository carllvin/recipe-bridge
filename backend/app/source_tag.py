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
import os
import re
import threading
import unicodedata
import uuid
from urllib.parse import urlparse

from . import llm_provider, tandoor_client, tool_jobs
from .config import settings
from .schemas import TokenUsage, ToolSuggestion

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
    remember_tag(tag)


# ---------- the tile "recipes without source" (existing recipes) ----------
#
# A recipe has a source when it has an address (source_url) or is in a
# cookbook (Tandoor's recipe books, Mealie's categories - an import's
# cookbook becomes one). What tag each source gets is remembered in the
# data folder - a site's name is looked up once, a book's author asked
# once - so the overview can tell, without any request, which recipes
# lack it. "" means: looked, nothing found - those recipes don't count.

METRIC = "recipes_without_source"
_lock = threading.Lock()


def _path() -> str:
    return os.path.join(settings.data_dir, "source_tags.json")


def known() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {"hosts": data.get("hosts") or {}, "books": data.get("books") or {}, "tags": data.get("tags") or []}


def remember(kind: str, key: str, tag: str) -> None:
    """kind "hosts" (an address's host) or "books" (a cookbook's name)."""
    if not key:
        return
    with _lock:
        data = known()
        data[kind][key] = tag
        os.makedirs(settings.data_dir, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _path())


def remember_tag(tag: str) -> None:
    """Every source tag the app gave - so the tag groups can tell them from
    other tags (and put them together)."""
    if not tag or tag.casefold() in all_tags():
        return
    with _lock:
        data = known()
        data["tags"].append(tag)
        os.makedirs(settings.data_dir, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _path())


def learn_from(recipes) -> None:
    """Source tags given before they were remembered: a recipe's tag whose
    name is in its address ("einfachkochen" on einfachkochen.de)."""
    known_tags = all_tags()
    for recipe in recipes:
        host = re.sub(r"[^a-z0-9]", "", host_of(recipe.get("source_url") or recipe.get("orgURL") or ""))
        if not host:
            continue
        for kw in recipe.get("keywords") or recipe.get("tags") or []:
            name = (kw.get("name") or "").strip()
            key = re.sub(r"[^a-z0-9]", "", name.casefold())
            if len(key) >= 4 and key in host and name.casefold() not in known_tags:
                remember_tag(name)
                known_tags.add(name.casefold())


def all_tags() -> set[str]:
    """The source tags the app knows (casefolded): given on import, or
    remembered for a site or cookbook."""
    data = known()
    return {t.casefold() for t in [*data["tags"], *data["hosts"].values(), *data["books"].values()] if t}


def host_of(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


def missing(url: str, books: list[str], keywords: list[str], store: dict | None = None):
    """The source a recipe lacks its tag for - ("hosts", host) or ("books",
    name) - or None (tagged, nothing found for it, or no source at all)."""
    store = store or known()
    have = {k.strip().casefold() for k in keywords}
    host = host_of(url)
    if host:
        tag = store["hosts"].get(host)
        if tag == "" or (tag or from_address(url)).casefold() in have or from_address(url) in have:
            return None
        return ("hosts", host)
    for book in books:
        tag = store["books"].get(book)
        if tag == "":
            continue
        if tag is not None and tag.casefold() in have:
            return None
        return ("books", book)
    return None


def site_tag(url: str) -> str:
    """A site's own name, read from one of its pages - else its address."""
    from bs4 import BeautifulSoup

    from .url_processor import _client
    try:
        with _client() as client:
            resp = client.get(url, timeout=10)
        if resp.status_code == 200:
            tag = from_page(BeautifulSoup(resp.text[:300000], "html.parser"), url, None)
            if tag:
                return tag
    except Exception as exc:  # noqa: BLE001 - the address still names it
        log.info("Site name of %s not readable: %s", url, exc)
    return from_address(url)


BOOK_PROMPT = """A cookbook in a recipe collection is called "{book}"; some of its
recipes: {titles}. Who wrote it? Only answer when you know this cookbook -
a name like "Dinner" or "Family recipes" is no book you know.

Respond with ONLY a JSON object: {{"author": "<first author's full name>" or null}}."""


def book_tag(book: str, titles: list[str]) -> tuple[str, TokenUsage]:
    if not llm_provider.is_configured():
        return "", TokenUsage()
    try:
        out, usage = llm_provider.complete_tool_text(
            None, BOOK_PROMPT.format(book=book, titles=", ".join(titles[:8])), max_tokens=60)
        match = re.search(r"\{.*\}", out or "", re.S)
        author = (json.loads(match.group(0)).get("author") if match else None) or ""
        return from_author(author if isinstance(author, str) else ""), usage or TokenUsage()
    except Exception as exc:  # noqa: BLE001
        log.info("Author of %r not found: %s", book, exc)
        return "", TokenUsage()


def suggestions(job, recipes: list[dict]) -> list[ToolSuggestion]:
    """recipes: {"id", "name", "source_url", "books", "keywords"} - one
    suggestion per source: its tag for all its recipes."""
    store = known()
    groups: dict[tuple, list[dict]] = {}
    for r in recipes:
        key = missing(r.get("source_url") or "", r.get("books") or [], [k["name"] for k in r.get("keywords") or []], store)
        if key:
            groups.setdefault(key, []).append(r)
    job.progress_total = len(groups)
    out = []
    for n, ((kind, key), members) in enumerate(sorted(groups.items()), 1):
        if job.cancel_requested:
            break
        job.progress_current = n
        job.progress_label = f"Source {n}/{len(groups)}: {key}"
        tool_jobs.save_tool_job(job)
        tag = store[kind].get(key)
        if tag is None:
            if kind == "hosts":
                tag = site_tag(members[0]["source_url"])
            else:
                tag, usage = book_tag(key, [m["name"] for m in members])
                job.token_usage.add(usage)
            remember(kind, key, tag)
        if not tag:
            continue
        names = sorted(m["name"] for m in members)
        out.append(ToolSuggestion(
            id=uuid.uuid4().hex[:10], kind="source_tag",
            summary=f"tag '{tag}' for {len(members)} recipe(s) from {key}",
            preview="\n".join(names[:30]) + (f"\n… and {len(names) - 30} more" if len(names) > 30 else ""),
            detail={"tag": tag, "recipe_ids": [m["id"] for m in members], "source": key}))
    return out


def run_scan(job_id: str) -> None:
    """Tandoor: the tile's tool."""
    from . import ignored, recipe_scope
    from .migration import _tandoor_books
    job = tool_jobs.get_tool_job(job_id)
    if job is None:
        return
    try:
        with tandoor_client.get_client() as client:
            job.progress_label = "Looking for recipes without a source tag..."
            tool_jobs.save_tool_job(job)
            books = _tandoor_books(client)
            store = known()
            todo = recipe_scope.recipes_for(
                client, METRIC, lambda r: missing(r.get("source_url") or "", books.get(r["id"], []),
                                                  [k["name"] for k in r.get("keywords") or []], store) is not None,
                ignored.keys(METRIC), job)
            job.suggestions = suggestions(job, [{**r, "books": books.get(r["id"], [])} for r in todo])
        job.status = "cancelled" if job.cancel_requested else "ready"
        job.progress_label = None
        tool_jobs.save_tool_job(job)
    except Exception as exc:  # noqa: BLE001
        log.exception("Source tag scan failed for job %s", job_id)
        job.status = "error"
        job.error = str(exc)
        tool_jobs.save_tool_job(job)


def apply_suggestion(job_id: str, suggestion_id: str) -> ToolSuggestion:
    """Tandoor: the tag onto every recipe of the source."""
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
            tag_id, _name = tandoor_client._get_or_create(client, "keyword", suggestion.detail["tag"])
            for recipe_id in suggestion.detail["recipe_ids"]:
                resp = client.get(f"/recipe/{recipe_id}/")
                resp.raise_for_status()
                ids = [kw["id"] for kw in resp.json().get("keywords", [])]
                if tag_id in ids:
                    continue
                resp = client.patch(f"/recipe/{recipe_id}/", json={"keywords": [{"id": k} for k in ids + [tag_id]]})
                if resp.status_code not in (200, 201):
                    raise tandoor_client.TandoorError(f"{resp.status_code} {resp.text[:300]}")
        suggestion.status = "applied"
    except Exception as exc:  # noqa: BLE001
        suggestion.status = "error"
        suggestion.error = str(exc)
    finally:
        tool_jobs.save_tool_job(job)
    return suggestion
