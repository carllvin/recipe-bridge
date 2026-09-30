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
import uuid
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from PIL import Image

from . import video_processor

log = logging.getLogger("recipe-bridge")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "de,en;q=0.8",
}
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 30000


class UrlImportError(Exception):
    pass


MAX_LINKS = 50
_URL_RE = re.compile(r"https?://[^\s<>\"'\])]+")


def links_from_text(text: str) -> list[str]:
    """Every http(s) link in a text file (one per line, or anywhere in the
    text), without duplicates, in their order, at most MAX_LINKS."""
    links = []
    for match in _URL_RE.findall(text or ""):
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
    t = node.get("@type") if isinstance(node, dict) else None
    return {t} if isinstance(t, str) else set(t or [])


def _find_recipe(data):
    """Depth-first search for a schema.org Recipe object in JSON-LD
    (top level, in lists, or inside an @graph)."""
    if isinstance(data, list):
        for item in data:
            found = _find_recipe(item)
            if found:
                return found
    elif isinstance(data, dict):
        if "Recipe" in _types(data):
            return data
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in data:
                found = _find_recipe(data[key])
                if found:
                    return found
    return None


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
        elif isinstance(item, dict) and "HowToSection" in _types(item):
            if item.get("name"):
                lines.append(f"## {_text(item['name'])}")
            lines.extend(_instructions(item.get("itemListElement")))
        elif isinstance(item, dict):
            lines.append(_text(item.get("text") or item.get("name") or ""))
    return [line for line in lines if line]


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
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    lines = [line.strip() for line in main.get_text("\n").split("\n") if line.strip()]
    return "\n".join(lines)[:MAX_TEXT_CHARS]


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


def _save_image(client: httpx.Client, url: str, images_dir: str) -> dict | None:
    try:
        resp = client.get(url)
        resp.raise_for_status()
        if len(resp.content) > MAX_IMAGE_BYTES:
            return None
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.info("Recipe image %s not usable: %s", url, exc)
        return None
    image_id = uuid.uuid4().hex[:12]
    filename = f"{image_id}.jpg"
    out_path = os.path.join(images_dir, filename)
    img.save(out_path, "JPEG", quality=90)
    return {"id": image_id, "page": 1, "path": out_path, "filename": filename, "width": img.width, "height": img.height}


def _client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=20)


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
            "metadata_title": info.get("title") or "", "toc_pages": []}


def process_url(url: str, images_dir: str) -> dict:
    os.makedirs(images_dir, exist_ok=True)
    if video_processor.platform(url):
        return _process_video(url, images_dir)
    with _client() as client:
        try:
            resp = client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise UrlImportError(f"Could not load the page: {exc}") from exc
        if len(resp.content) > MAX_PAGE_BYTES:
            raise UrlImportError("The page is too large.")
        soup = BeautifulSoup(resp.text, "html.parser")

        recipe = None
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                recipe = _find_recipe(json.loads(script.string or script.get_text() or ""))
            except (json.JSONDecodeError, TypeError):
                continue
            if recipe:
                break

        if recipe:
            text = _recipe_to_text(recipe, url)
            title = _text(recipe.get("name"))
        else:
            log.info("No schema.org Recipe on %s - using the page text", url)
            title = soup.title.get_text(strip=True) if soup.title else ""
            text = f"Source: {url}\n\n" + _visible_text(BeautifulSoup(resp.text, "html.parser"))
        if len(text.strip()) < 50:
            raise UrlImportError("No recipe text found on that page.")

        images = {}
        image_url = _image_url(recipe, soup, str(resp.url))
        if image_url:
            saved = _save_image(client, image_url, images_dir)
            if saved:
                images[saved.pop("id")] = saved

    return {
        "pages": [{"page": 1, "text": text}],
        "images": images,
        "page_count": 1,
        "metadata_title": title,
        "toc_pages": [],
    }
