"""Weak recipe photos - found without AI, improved with it.

measure() looks at the pixels: too dark, too bright, washed out (little
contrast), blurry or too small. That costs nothing; the AI is only used for
the photos you choose to improve (image_gen.enhance_image), each paid when
its suggestion is applied.

Each photo is downloaded and measured once - the result is kept in
photo_quality.json in the data volume, keyed by the photo's address (a
replaced photo gets a new one). The health overview measures at most
MAX_NEW_PER_RUN new photos per refresh, the rest follow next time."""
from __future__ import annotations

import io
import json
import logging
import os
import threading

from .config import settings

log = logging.getLogger("recipe-bridge")

MAX_NEW_PER_RUN = 200
MEASURE_SIDE = 512
# Thresholds (0-255 grey values); deliberately conservative - only clear cases.
DARK_BELOW = 60
BRIGHT_ABOVE = 225
FLAT_BELOW = 22        # standard deviation of the grey values
BLURRY_BELOW = 40      # the strongest edges (99th percentile) - a sharp photo has some, whatever it shows
SMALL_BELOW = 400      # shorter side in pixels

_lock = threading.Lock()


def _path() -> str:
    return os.path.join(settings.data_dir, "photo_quality.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(cache: dict) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    os.replace(tmp, _path())


def measure(data: bytes) -> dict:
    """{"brightness", "contrast", "sharpness", "short_side", "problems": [...]}"""
    from PIL import Image, ImageFilter, ImageOps, ImageStat

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    short_side = min(img.size)
    grey = img.convert("L")
    grey.thumbnail((MEASURE_SIDE, MEASURE_SIDE))
    stat = ImageStat.Stat(grey)
    brightness, contrast = stat.mean[0], stat.stddev[0]
    sharpness = _percentile(grey.filter(ImageFilter.FIND_EDGES).histogram(), 0.99)
    problems = []
    if brightness < DARK_BELOW:
        problems.append("dark")
    elif brightness > BRIGHT_ABOVE:
        problems.append("bright")
    if contrast < FLAT_BELOW:
        problems.append("flat")
    if sharpness < BLURRY_BELOW:
        problems.append("blurry")
    if short_side < SMALL_BELOW:
        problems.append("small")
    return {"brightness": round(brightness, 1), "contrast": round(contrast, 1), "sharpness": round(sharpness, 1),
            "short_side": short_side, "problems": problems}


def _percentile(histogram, share) -> int:
    total, seen = sum(histogram), 0
    for value, count in enumerate(histogram):
        seen += count
        if seen >= share * total:
            return value
    return len(histogram) - 1


def problems_of(photos: dict, fetch) -> dict:
    """photos: {key: photo address}; fetch(address) -> bytes. Returns
    {key: [problems]} for the measured photos with problems - from the cache,
    measuring up to MAX_NEW_PER_RUN new ones."""
    with _lock:
        cache = _load()
        new = 0
        for address in photos.values():
            if address in cache or new >= MAX_NEW_PER_RUN:
                continue
            new += 1
            try:
                cache[address] = measure(fetch(address))["problems"]
            except Exception as exc:  # noqa: BLE001 - a photo that can't be read is just skipped
                log.info("Photo %s not measured: %s", address, exc)
                cache[address] = []
        if new:
            # forget photos that are gone (replaced or deleted)
            current = set(photos.values())
            cache = {a: p for a, p in cache.items() if a in current}
            _save(cache)
    return {key: cache[address] for key, address in photos.items() if cache.get(address)}


def forget(address: str) -> None:
    with _lock:
        cache = _load()
        if cache.pop(address, None) is not None:
            _save(cache)


LABELS = {
    "de": {"dark": "zu dunkel", "bright": "zu hell", "flat": "flau", "blurry": "unscharf", "small": "zu klein"},
    "en": {"dark": "too dark", "bright": "too bright", "flat": "washed out", "blurry": "blurry", "small": "too small"},
    "fr": {"dark": "trop sombre", "bright": "trop clair", "flat": "terne", "blurry": "flou", "small": "trop petit"},
    "it": {"dark": "troppo scura", "bright": "troppo chiara", "flat": "piatta", "blurry": "sfocata", "small": "troppo piccola"},
    "es": {"dark": "muy oscura", "bright": "muy clara", "flat": "apagada", "blurry": "borrosa", "small": "muy pequeña"},
}


def describe(problems) -> str:
    from .config import get_language_code
    labels = LABELS.get(get_language_code(settings.output_language), LABELS["en"])
    return ", ".join(labels.get(p, p) for p in problems)


def mealie_address(recipe) -> str | None:
    """The original photo of a Mealie recipe; the image key changes with
    every new photo, so it's part of the address (and the cache key)."""
    if not recipe.get("image") or not recipe.get("id"):
        return None
    return f"/media/recipes/{recipe['id']}/images/original.webp?v={recipe['image']}"


def fetcher(client):
    def fetch(address):
        resp = client.get(address)
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return resp.content
    return fetch
