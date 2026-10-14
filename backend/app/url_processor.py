"""Turns a recipe web page into the same shape as pdf_processor /
epub_processor / image_processor, so a single recipe from a URL runs
through the normal extraction, review and import (translation, metric
conversion, ingredients per step, matching against Tandoor ...).

Most recipe sites embed schema.org "Recipe" data (JSON-LD) - that's used
when present, since it's clean and complete. Otherwise the page's visible
main text is used. The recipe's photo (JSON-LD image or og:image) becomes a
candidate image."""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
import uuid
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from . import source_tag, video_processor

log = logging.getLogger("recipe-bridge")

# What a browser sends - some sites refuse requests that look like a script.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
# A second try when a site refuses the first one (bot protection).
RETRY_HEADERS = {
    **HEADERS,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Referer": "https://www.google.com/",
    "Sec-Fetch-Site": "cross-site",
}
BLOCKED_STATUS = {401, 403, 429, 503}
BLOCKED_HINT = ("The site blocks automatic access (HTTP {status}). Open the recipe in your browser and use the "
                "📥 bookmark button there (under More ways) - it sends the page from your browser - or copy the "
                "recipe text and paste it.")
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_STEP_IMAGES = 15   # photos the page shows with its steps (image_steps.py)
# Bot protection often lets a request hang instead of refusing it - so short
# waits, and the photos of a recipe share one time budget.
PAGE_TIMEOUT = httpx.Timeout(15, connect=8)
IMAGE_TIMEOUT = httpx.Timeout(8, connect=5)
PHOTOS_SECONDS = 30
MAX_TEXT_CHARS = 30000


class UrlImportError(Exception):
    pass


MAX_LINKS = 50
_URL_RE = re.compile(r"https?://[^\s<>\"'\])]+")


def links_from_text(text: str) -> list[str]:
    """Every http(s) link in a text file (one per line, or anywhere in the
    text), without duplicates, in their order, at most MAX_LINKS."""
    links = []
    text = re.sub(r",\s*(?=https?://)", "\n", text or "")  # "https://a.example/x,https://b.example/y"
    for match in _URL_RE.findall(text):
        url = match.rstrip(".,;:!?")
        if url not in links:
            links.append(url)
    return links[:MAX_LINKS]


def validate_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UrlImportError("Please enter a full web address starting with http:// or https://")
    return url


def _types(node) -> set[str]:
    """The node's schema.org types, lower case and without a prefix - pages
    write "recipe" or "http://schema.org/Recipe" too."""
    t = node.get("@type") if isinstance(node, dict) else None
    names = [t] if isinstance(t, str) else [x for x in (t or []) if isinstance(x, str)]
    return {n.rsplit("/", 1)[-1].rsplit(":", 1)[-1].strip().lower() for n in names}


def _find_recipe(data, kind: str = "Recipe"):
    """Depth-first search for a schema.org Recipe object (or another type)
    in JSON-LD (top level, in lists, or inside an @graph)."""
    if isinstance(data, list):
        for item in data:
            found = _find_recipe(item, kind)
            if found:
                return found
    elif isinstance(data, dict):
        if kind.lower() in _types(data):
            return data
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in data:
                found = _find_recipe(data[key], kind)
                if found:
                    return found
    return None


def _howto_as_recipe(howto: dict) -> dict:
    """A schema.org HowTo ("how to make fresh pasta" step-by-step guides) read
    like a recipe: its supplies are the ingredients, its steps the method."""
    supplies = [s.get("name") if isinstance(s, dict) else s for s in _as_list(howto.get("supply"))]
    return {**howto, "recipeIngredient": [_text(s) for s in supplies if s],
            "recipeInstructions": howto.get("step") or [], "recipeYield": howto.get("yield")}


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value] if value else []


def _text(value) -> str:
    if isinstance(value, str):
        return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(_text(v) for v in value if v)
    if isinstance(value, dict):
        return _text(value.get("text") or value.get("name") or "")
    return ""


def _instructions(value) -> list[str]:
    """recipeInstructions can be a string, a list of strings, HowToStep
    objects, or HowToSection objects containing steps."""
    if isinstance(value, str):
        return [line.strip() for line in _text(value).split("\n") if line.strip()] or [_text(value)]
    lines = []
    for item in value or []:
        if isinstance(item, str):
            lines.append(_text(item))
        elif isinstance(item, dict) and "howtosection" in _types(item):
            if item.get("name"):
                lines.append(f"## {_text(item['name'])}")
            lines.extend(_instructions(item.get("itemListElement")))
        elif isinstance(item, dict):
            lines.append(_text(item.get("text") or item.get("name") or ""))
    return [line for line in lines if line]


def _image_of(value) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl")
    return value if isinstance(value, str) and value.strip() else None


def _step_images(value) -> tuple[list[tuple[int, str]], int]:
    """([(step position 0.., image url)], number of steps) from the
    HowToSteps of recipeInstructions - the photos the page shows with a step."""
    found, count = [], 0

    def walk(items):
        nonlocal count
        for item in items or []:
            if isinstance(item, dict) and "howtosection" in _types(item):
                walk(item.get("itemListElement"))
            elif isinstance(item, (dict, str)):
                image = _image_of(item.get("image")) if isinstance(item, dict) else None
                if image:
                    found.append((count, image))
                count += 1
    if isinstance(value, list):
        walk(value)
    return found, count


def _recipe_to_text(recipe: dict, url: str) -> str:
    parts = [f"Recipe: {_text(recipe.get('name'))}", f"Source: {url}"]
    if recipe.get("description"):
        parts.append(f"Description: {_text(recipe['description'])}")
    for key, label in (("recipeYield", "Servings"), ("prepTime", "Prep time"), ("cookTime", "Cook time"),
                       ("totalTime", "Total time"), ("recipeCategory", "Category"), ("recipeCuisine", "Cuisine"),
                       ("keywords", "Keywords")):
        if recipe.get(key):
            parts.append(f"{label}: {_text(recipe[key])}")
    parts.append("\nIngredients:")
    parts.extend(f"- {_text(i)}" for i in recipe.get("recipeIngredient") or recipe.get("ingredients") or [])
    parts.append("\nInstructions:")
    n = 0
    for line in _instructions(recipe.get("recipeInstructions")):
        if line.startswith("## "):
            parts.append(line[3:] + ":")  # section heading, not a step
        else:
            n += 1
            parts.append(f"{n}. {line}")
    return "\n".join(parts)


def _visible_text(soup: BeautifulSoup) -> str:
    """The page's main text: the article/main element with the most text -
    not just the first one (often a teaser card) - or the whole page when
    none of them holds most of it."""
    # not <form>: some sites (ASP.NET) put the whole page into one
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "svg",
                     "input", "select", "button", "textarea"]):
        tag.decompose()
    body = soup.body or soup

    def lines(node):
        return [line.strip() for line in node.get_text("\n").split("\n") if line.strip()]
    whole = lines(body)
    best = max((lines(n) for n in soup.find_all(["article", "main"]) + soup.select("[role=main]")),
               key=lambda ls: sum(map(len, ls)), default=[])
    chosen = best if sum(map(len, best)) >= 0.4 * sum(map(len, whole)) else whole
    return "\n".join(chosen)[:MAX_TEXT_CHARS]


def _article_body(soup: BeautifulSoup) -> str:
    """articleBody of a schema.org Article in the page's JSON-LD, if any."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for kind in ("Article", "NewsArticle", "BlogPosting"):
            found = _find_recipe(data, kind)
            if found and found.get("articleBody"):
                return _text(found["articleBody"])
    return ""


def _image_url(recipe: dict | None, soup: BeautifulSoup, base: str) -> str | None:
    candidates = []
    if recipe:
        image = recipe.get("image")
        if isinstance(image, str):
            candidates.append(image)
        elif isinstance(image, dict):
            candidates.append(image.get("url"))
        elif isinstance(image, list):
            for item in image:
                candidates.append(item.get("url") if isinstance(item, dict) else item)
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        candidates.append(og["content"])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return urljoin(base, candidate.strip())
    return None


def _save_image(client: httpx.Client, url: str, images_dir: str, gate: dict | None = None) -> dict | None:
    """gate: {"until": deadline, "blocked": hosts, "given": {url: bytes}}
    shared by a recipe's photos - a photo the browser already sent isn't
    loaded; past the deadline, or once a host timed out or refused, the
    rest of them aren't tried."""
    host = urlparse(url).netloc
    given = (gate or {}).get("given", {}).get(url)
    if given is None and gate is not None and (time.monotonic() > gate["until"] or host in gate["blocked"]):
        return None
    try:
        if given is not None:
            content = given
        else:
            resp = client.get(url, timeout=IMAGE_TIMEOUT)
            if gate is not None and resp.status_code in BLOCKED_STATUS:
                gate["blocked"].add(host)
            resp.raise_for_status()
            content = resp.content
        if len(content) > MAX_IMAGE_BYTES:
            return None
        img = Image.open(io.BytesIO(content)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        if gate is not None and isinstance(exc, httpx.TimeoutException):
            gate["blocked"].add(host)
        log.info("Recipe image %s not usable: %s", url, exc)
        return None
    image_id = uuid.uuid4().hex[:12]
    filename = f"{image_id}.jpg"
    out_path = os.path.join(images_dir, filename)
    img.save(out_path, "JPEG", quality=90)
    return {"id": image_id, "page": 1, "path": out_path, "filename": filename, "width": img.width, "height": img.height}


def _client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=PAGE_TIMEOUT)


def _process_video(url: str, images_dir: str) -> dict:
    """YouTube / Instagram / TikTok: description or caption plus - for
    YouTube - the subtitles (video_processor)."""
    from .config import get_language_code, settings
    with _client() as client:
        try:
            info = video_processor.read(client, url, get_language_code(settings.output_language))
        except httpx.HTTPError as exc:
            raise UrlImportError(f"Could not load the video page: {exc}") from exc
        if len((info.get("description") or "") + (info.get("transcript") or "")) < 40:
            raise UrlImportError(
                "The video's text couldn't be read (the site may want a login or consent). "
                "Copy the recipe from the video description and paste it as text instead.")
        images = {}
        if info.get("image"):
            saved = _save_image(client, info["image"], images_dir)
            if saved:
                images[saved.pop("id")] = saved
    return {"pages": [{"page": 1, "text": info["text"]}], "images": images, "page_count": 1,
            "metadata_title": info.get("title") or "", "toc_pages": [],
            "source_tag": source_tag.slug(info.get("author") or "")}  # the channel


def _fetch_page(client: httpx.Client, url: str) -> tuple[str, str]:
    """(html, final url) - a second try with other headers when a site's bot
    protection refuses the first."""
    try:
        resp = client.get(url, timeout=PAGE_TIMEOUT)
        if resp.status_code in BLOCKED_STATUS:
            resp = client.get(url, headers=RETRY_HEADERS, timeout=PAGE_TIMEOUT)
        if resp.status_code in BLOCKED_STATUS:
            raise UrlImportError(BLOCKED_HINT.format(status=resp.status_code))
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise UrlImportError("The site doesn't answer (it may block automatic access). Use the 📥 bookmark "
                             "button on the recipe page (under More ways) or paste the recipe text.") from exc
    except httpx.HTTPError as exc:
        raise UrlImportError(f"Could not load the page: {exc}") from exc
    if len(resp.content) > MAX_PAGE_BYTES:
        raise UrlImportError("The page is too large.")
    return resp.text, str(resp.url)


def _page_recipe(soup: BeautifulSoup) -> tuple[dict | None, dict | None]:
    """(the page's schema.org recipe with ingredients or method, a Recipe
    entry with only name, image, address - its photo is still good)."""
    recipe = None
    scripts = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            scripts.append(json.loads(script.string or script.get_text() or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    stub = None
    for kind in ("Recipe", "HowTo"):  # a recipe first, else a step-by-step guide
        found = next((r for r in (_find_recipe(d, kind) for d in scripts) if r), None)
        if found and kind == "HowTo":
            found = _howto_as_recipe(found)
        if found and (found.get("recipeIngredient") or found.get("ingredients")
                      or _instructions(found.get("recipeInstructions"))):
            recipe = found
            break
        stub = stub or found
    return recipe, stub


def _photo_urls(recipe, stub, soup, page_url) -> tuple[str | None, list[tuple[int, str]], int]:
    """(main photo, [(step position, step photo)], number of original steps)."""
    step_images, steps = _step_images(recipe.get("recipeInstructions")) if recipe else ([], 0)
    return (_image_url(recipe or stub, soup, page_url),
            [(position, urljoin(page_url, u)) for position, u in step_images[:MAX_STEP_IMAGES]], steps)


def page_photo_urls(url: str, html: str) -> list[str]:
    """The photos an import of this page would load - the bookmark button
    lets the browser fetch them (sites that refuse the app their photos)."""
    soup = BeautifulSoup(html or "", "html.parser")
    recipe, stub = _page_recipe(soup)
    main, steps, _ = _photo_urls(recipe, stub, soup, url)
    return list(dict.fromkeys(([main] if main else []) + [u for _, u in steps]))


# ---------- short links and Pinterest pins: on to the recipe page ----------

SHORTENERS = {"bit.ly", "bitly.com", "t.co", "tinyurl.com", "ow.ly", "buff.ly", "goo.gl", "is.gd", "rebrand.ly",
              "cutt.ly", "shorturl.at", "tiny.cc", "t.ly", "s.id", "lnkd.in", "fb.me", "trib.al", "dlvr.it",
              "pin.it", "spoti.fi", "amzn.to", "flip.it", "smarturl.it", "linktr.ee"}
_PIN_LINK_RE = re.compile(r'"(?:link|tracked_link)"\s*:\s*"(https?:(?:\\/|/)[^"]+)"')


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _is_pinterest(url: str) -> bool:
    host = _host(url)
    return host.startswith("pinterest.") or ".pinterest." in host or host in ("pin.it", "pinimg.com") \
        or host.endswith(".pinimg.com")


def _unshorten(client: httpx.Client, url: str) -> str:
    """Follows a short link's redirects without loading the page."""
    try:
        with client.stream("GET", url, timeout=PAGE_TIMEOUT) as resp:
            return str(resp.url)
    except httpx.HTTPError as exc:
        log.info("Short link %s not resolved: %s", url, exc)
        return url


def pin_target(html: str) -> str | None:
    """The page a Pinterest pin links to - from og:see_also, the JSON-LD
    post or the page's own data; None for a pin without a link."""
    soup = BeautifulSoup(html or "", "html.parser")
    candidates = [m.get("content") for m in soup.find_all("meta", attrs={"property": "og:see_also"})]
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            shared = item.get("sharedContent") if isinstance(item, dict) else None
            if isinstance(shared, dict):
                candidates.append(shared.get("url"))
    candidates += [m.replace("\\/", "/").replace("\\u002F", "/") for m in _PIN_LINK_RE.findall(html or "")]
    for link in candidates:
        if not link or _is_pinterest(link) or not link.startswith("http"):
            continue
        if re.search(r"\.(jpe?g|png|webp|gif|mp4)(\?|$)", link, re.I):  # the pin's own picture
            continue
        return link
    return None


def _resolve(client: httpx.Client, url: str, html: str | None) -> tuple[str, str | None]:
    """(the recipe page's address, the page as given) - short links are
    followed, a Pinterest pin leads to the page it was saved from (the pin
    itself is read when it has no link)."""
    if html is None and _host(url) in SHORTENERS:
        url = _unshorten(client, url)
    if _is_pinterest(url) and "/pin/" in urlparse(url).path:
        pin_html = html
        if pin_html is None:
            try:
                pin_html, _ = _fetch_page(client, url)
            except UrlImportError as exc:
                log.info("Pinterest pin %s not loaded: %s", url, exc)
                return url, html
        target = pin_target(pin_html)
        if target:
            log.info("Pinterest pin %s links to %s", url, target)
            target = _unshorten(client, target) if _host(target) in SHORTENERS else target
            return target, None
        return url, pin_html
    return url, html


def process_url(url: str, images_dir: str, html: str | None = None,
                given_photos: dict[str, bytes] | None = None) -> dict:
    """html: the page as the browser already has it (bookmark button) - then
    it isn't loaded again (sites with bot protection refuse that); its
    photos too, as far as the browser could fetch them (given_photos:
    {address: bytes}) - the others are loaded as usual."""
    os.makedirs(images_dir, exist_ok=True)
    with _client() as client:
        url, html = _resolve(client, url, html)
    if video_processor.platform(url) and html is None:
        return {**_process_video(url, images_dir), "url": url}
    with _client() as client:
        if html is None:
            html, page_url = _fetch_page(client, url)
        else:
            page_url = url
            if len(html.encode("utf-8", "ignore")) > MAX_PAGE_BYTES:
                raise UrlImportError("The page is too large.")
        soup = BeautifulSoup(html, "html.parser")

        recipe, stub = _page_recipe(soup)

        def page_text():
            visible = _visible_text(BeautifulSoup(html, "html.parser"))
            article = _article_body(soup)
            return article if len(article) > len(visible) else visible

        if recipe:
            text = _recipe_to_text(recipe, url)
            title = _text(recipe.get("name"))
            if not _instructions(recipe.get("recipeInstructions")):  # the method only on the page
                text += "\n\nPage text:\n" + page_text()
        else:
            log.info("No complete schema.org Recipe on %s - using the page text", url)
            title = _text((stub or {}).get("name")) or (soup.title.get_text(strip=True) if soup.title else "")
            text = f"Source: {url}\n\n" + page_text()
        if len(text.strip()) < 50:
            raise UrlImportError("No recipe text found on that page.")

        images = {}
        gate = {"until": time.monotonic() + PHOTOS_SECONDS, "blocked": set(), "given": given_photos or {}}
        image_url, step_images, steps = _photo_urls(recipe, stub, soup, page_url)
        if image_url:
            saved = _save_image(client, image_url, images_dir, gate)
            if saved:
                images[saved.pop("id")] = saved
        for order, (position, step_url) in enumerate(step_images):
            saved = _save_image(client, step_url, images_dir, gate)
            if saved:
                images[saved.pop("id")] = {**saved, "step": position, "of": steps, "order": order}

    return {
        "pages": [{"page": 1, "text": text}],
        "images": images,
        "page_count": 1,
        "metadata_title": title,
        "toc_pages": [],
        "source_tag": source_tag.from_page(soup, page_url, recipe or stub),
        "url": url,  # the recipe page - also when a short link or a Pinterest pin led there
    }
