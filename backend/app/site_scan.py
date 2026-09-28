"""Scans a website for recipe pages and returns a list to pick from - no
AI involved. Only the recipes picked afterwards go through the (AI) link
import.

How pages are found:
- a start URL without a path (the homepage): the site's sitemaps first
  (from robots.txt, or the usual /sitemap.xml locations), recipe-looking
  URLs first;
- a start URL with a path (e.g. a category page) - or no sitemap: follow the
  links on that page (same domain), 1-4 levels deep (chosen per scan, two
  by default), pagination included.
A page counts as a recipe when it embeds schema.org Recipe data (JSON-LD),
which nearly every recipe site does; title, photo and time come from there.

Polite by design: robots.txt is respected, one request per REQUEST_DELAY
seconds, at most MAX_CHECK_BY_DEPTH pages per scan, same domain only."""
from __future__ import annotations

import difflib
import json
import logging
import re
import threading
import time
import uuid
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

from . import tandoor_client
from .url_processor import HEADERS, MAX_PAGE_BYTES, _find_recipe, _image_url, _text

log = logging.getLogger("tandoor-helper")

REQUEST_DELAY = 1.0
MAX_SITEMAPS = 15
# How many link levels are followed from the start page (chosen per scan),
# and how many pages are checked at most for that depth.
DEFAULT_DEPTH = 2
MAX_CHECK_BY_DEPTH = {1: 150, 2: 300, 3: 600, 4: 1000}
USER_AGENT_TOKEN = "TandoorHelper"
RECIPE_HINTS = ("rezept", "recipe", "recette", "ricetta", "receta", "recept")
SKIP_PARTS = ("/tag/", "/author/", "/autor/", "/wp-admin", "/wp-json", "/feed", "/login", "/cart", "/warenkorb",
              "/impressum", "/datenschutz", "/privacy", "/kontakt", "/contact", "/search", "/suche", "?s=", "/comment")
SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|zip|mp4|mp3|css|js|xml|ico)(\?|$)", re.I)

_scans: dict[str, dict] = {}
_lock = threading.Lock()


def _fetch(client, url, **kw):
    """GET within the page size limit; None on any failure."""
    try:
        with client.stream("GET", url, **kw) as resp:
            if resp.status_code != 200:
                return None
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > MAX_PAGE_BYTES:
                    return None
            return resp.url, body.decode(resp.encoding or "utf-8", errors="replace"), resp.headers.get("content-type", "")
    except httpx.HTTPError:
        return None


def _same_site(url, host) -> bool:
    return urlparse(url).netloc.lower().removeprefix("www.") == host


def _clean(url) -> str:
    return urldefrag(url)[0]


def _worth_checking(url) -> bool:
    low = url.lower()
    return not SKIP_EXT.search(low) and not any(p in low for p in SKIP_PARTS)


def _recipe_first(urls):
    return sorted(urls, key=lambda u: 0 if any(h in u.lower() for h in RECIPE_HINTS) else 1)


def _minutes(value) -> int | None:
    """ISO 8601 duration (PT1H30M) -> minutes."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:\d+S)?", str(value or "").strip(), re.I)
    if not m or not any(m.groups()):
        return None
    days, hours, minutes = (int(g or 0) for g in m.groups())
    return days * 1440 + hours * 60 + minutes or None


def _sitemap_urls(client, sitemaps, host, state) -> list[str]:
    """Page URLs from sitemaps (following sitemap indexes, recipe-looking
    child sitemaps first)."""
    todo, seen, pages = list(sitemaps), set(), []
    while todo and len(seen) < MAX_SITEMAPS and not state["cancel"]:
        sm = todo.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        got = _fetch(client, sm)
        time.sleep(REQUEST_DELAY)
        if not got:
            continue
        try:
            root = ET.fromstring(got[1].encode("utf-8"))
        except ET.ParseError:
            continue
        locs = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
        if root.tag.endswith("sitemapindex"):
            todo = _recipe_first([l for l in locs if l not in seen]) + todo
        else:
            pages.extend(l for l in locs if _same_site(l, host) and _worth_checking(l))
    return list(dict.fromkeys(pages))


def _links(soup, base, host) -> list[str]:
    out = []
    for a in soup.find_all("a", href=True):
        url = _clean(urljoin(base, a["href"]))
        if url.startswith("http") and _same_site(url, host) and _worth_checking(url):
            out.append(url)
    return list(dict.fromkeys(out))


def _recipe_item(url, soup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            recipe = _find_recipe(json.loads(script.string or script.get_text() or ""))
        except (json.JSONDecodeError, TypeError):
            continue
        if recipe and _text(recipe.get("name")):
            return {
                "url": url,
                "title": _text(recipe.get("name"))[:200],
                "image": _image_url(recipe, soup, url),
                "minutes": _minutes(recipe.get("totalTime")) or (
                    (_minutes(recipe.get("prepTime")) or 0) + (_minutes(recipe.get("cookTime")) or 0)) or None,
                "description": _text(recipe.get("description"))[:200] or None,
            }
    return None


def _existing_titles() -> list[str]:
    try:
        with tandoor_client.get_client() as client:
            return [n.casefold() for n in tandoor_client.fetch_all_recipe_names(client)]
    except Exception as exc:  # noqa: BLE001
        log.info("Scan: could not load existing recipe names (%s)", exc)
        return []


def _in_tandoor(title, existing) -> bool:
    t = title.casefold()
    return t in existing or bool(difflib.get_close_matches(t, existing, n=1, cutoff=0.9))


def start(url: str, depth: int = DEFAULT_DEPTH) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Please enter a full web address starting with http:// or https://")
    try:
        depth = int(depth)
    except (TypeError, ValueError):
        depth = DEFAULT_DEPTH
    if depth not in MAX_CHECK_BY_DEPTH:
        raise ValueError(f"The depth must be between 1 and {max(MAX_CHECK_BY_DEPTH)}.")
    scan_id = uuid.uuid4().hex[:10]
    state = {"id": scan_id, "url": url, "status": "scanning", "phase": "start", "checked": 0, "total": 0,
             "found": [], "error": None, "cancel": False, "started_at": time.time(), "source": None,
             "depth": depth, "max_check": MAX_CHECK_BY_DEPTH[depth]}
    with _lock:
        _scans[scan_id] = state
    threading.Thread(target=_run, args=(state,), daemon=True).start()
    return scan_id


def get(scan_id: str) -> dict | None:
    state = _scans.get(scan_id)
    return {k: v for k, v in state.items() if k != "cancel"} if state else None


def cancel(scan_id: str) -> None:
    state = _scans.get(scan_id)
    if state:
        state["cancel"] = True


def _run(state) -> None:
    start_url = state["url"]
    parsed = urlparse(start_url)
    host = parsed.netloc.lower().removeprefix("www.")
    homepage = parsed.path in ("", "/")
    max_depth, max_check = state["depth"], state["max_check"]
    try:
        with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=20) as client:
            robots = robotparser.RobotFileParser()
            got = _fetch(client, f"{parsed.scheme}://{parsed.netloc}/robots.txt")
            robots.parse(got[1].splitlines() if got else [])
            allowed = lambda u: robots.can_fetch(USER_AGENT_TOKEN, u)  # noqa: E731
            if not allowed(start_url):
                raise ValueError("The site's robots.txt does not allow reading this page.")

            candidates: list[tuple[str, int]] = []  # (url, depth)
            if homepage:
                state["phase"] = "sitemap"
                sitemaps = robots.site_maps() or [f"{parsed.scheme}://{parsed.netloc}/{p}" for p in
                                                  ("sitemap.xml", "sitemap_index.xml", "wp-sitemap.xml")]
                candidates = [(u, max_depth) for u in _recipe_first(_sitemap_urls(client, sitemaps, host, state))]
            state["source"] = "sitemap" if candidates else "links"
            if not candidates:
                candidates = [(start_url, 0)]

            existing = _existing_titles()
            state["phase"] = "pages"
            seen, queue = set(), candidates
            state["total"] = min(len(queue), max_check)
            while queue and state["checked"] < max_check and not state["cancel"]:
                url, depth = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                if not allowed(url):
                    continue
                got = _fetch(client, url)
                state["checked"] += 1
                time.sleep(REQUEST_DELAY)
                if not got or "html" not in got[2]:
                    continue
                soup = BeautifulSoup(got[1], "html.parser")
                item = _recipe_item(str(got[0]), soup)
                if item:
                    if not any(f["url"] == item["url"] for f in state["found"]):
                        item["in_tandoor"] = _in_tandoor(item["title"], existing)
                        state["found"].append(item)
                elif depth < max_depth:
                    queued = {u for u, _ in queue}
                    queue.extend((u, depth + 1) for u in _recipe_first(_links(soup, str(got[0]), host))
                                 if u not in seen and u not in queued)
                    del queue[max_check * 3:]  # enough to choose from
                state["total"] = min(state["checked"] + len(queue), max_check)
        state["status"] = "cancelled" if state["cancel"] else "done"
    except Exception as exc:  # noqa: BLE001
        log.info("Site scan of %s failed: %s", start_url, exc)
        state["status"], state["error"] = "error", str(exc)
    finally:
        state["phase"] = None
        # keep only the latest few scans in memory
        with _lock:
            for old in sorted(_scans.values(), key=lambda s: s["started_at"])[:-10]:
                _scans.pop(old["id"], None)
