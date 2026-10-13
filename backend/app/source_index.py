"""Which recipe in the collection came from which address - so an import of
the same page is flagged as a duplicate even when its title differs (a
translated title, a site's renamed recipe). Tandoor's recipe list has no
addresses, so they are kept here: the overview (Maintain) writes them from
its full read, every import through this app adds its own."""
from __future__ import annotations

import json
import logging
import os
import threading
from urllib.parse import parse_qsl, urlencode, urlparse

from .config import settings

log = logging.getLogger("recipe-bridge")
_lock = threading.Lock()
TRACKING = ("utm_", "fbclid", "gclid", "mc_", "ref", "smid")


def _path() -> str:
    return os.path.join(settings.data_dir, "source_index.json")


def normalize(url: str) -> str:
    """The same page however it was linked: no scheme, no www, no trailing
    slash, no tracking parameters, no #anchor."""
    parts = urlparse((url or "").strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(TRACKING)]
    path = parts.path.rstrip("/") or ""
    return host + path + (f"?{urlencode(sorted(query))}" if query else "")


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _path())


def rebuild(pairs) -> None:
    """[(address, recipe name)] of the whole collection (the overview's read)."""
    data = {normalize(u): n for u, n in pairs if u and normalize(u)}
    with _lock:
        _save(data)


def add(url: str, name: str) -> None:
    key = normalize(url)
    if not key:
        return
    with _lock:
        data = load()
        data[key] = name
        _save(data)


def remove(url: str) -> None:
    key = normalize(url)
    with _lock:
        data = load()
        if data.pop(key, None) is not None:
            _save(data)


def find(url: str) -> str | None:
    """The name of the recipe already imported from this address."""
    key = normalize(url)
    return load().get(key) if key else None
