"""Undo for applied suggestions.

While a suggestion is applied, the Tandoor client records every write
(see tandoor_client.RecordingClient) as its inverse:
- PATCH/PUT /<endpoint>/<id>/ -> the object's previous values of exactly the
  fields that were sent ("restore"),
- DELETE /<endpoint>/<id>/     -> the whole object before deleting ("recreate"),
- POST /<endpoint>/            -> the new object's id ("delete"),
- PUT /<endpoint>/<id>/move/<parent>/ (tag/food trees) -> the previous
  parent ("move").
That journal is saved per suggestion in the data volume. Undoing replays it
backwards; objects that get recreated get a new id in Tandoor, so references
to the old id in later steps (an ingredient's food, a recipe's keywords ...)
are rewritten on the fly. Steps that succeeded are removed from the saved
journal, so a failed undo can simply be retried.

This works for every tool without tool-specific undo code. Limits: changes
made in Tandoor to the same objects since then are overwritten, and side
effects Tandoor does on its own (e.g. shopping-list entries created with a
meal-plan entry) are not tracked."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time

from .config import settings

log = logging.getLogger("recipe-bridge")

RETENTION_DAYS = 14

_local = threading.local()

_ITEM_PATH = re.compile(r"^/([a-z][a-z\-]*)/(\d+)/?$")
_MOVE_PATH = re.compile(r"^/([a-z][a-z\-]*)/(\d+)/move/(\d+)/?$")  # tree entries (tags, foods)
_LIST_PATH = re.compile(r"^/([a-z][a-z\-]*)/?$")

# Keys whose value is a reference to another object (keep its id), and the
# endpoint that id belongs to - used to rewrite ids of recreated objects.
REF_KEYS = {
    "food": "food", "unit": "unit", "keywords": "keyword", "keyword": "keyword",
    "supermarket_category": "supermarket-category", "category": "supermarket-category",
    "property_type": "property-type", "properties_food_unit": "unit", "base_unit": "unit",
    "converted_unit": "unit", "recipe": "recipe", "meal_type": "meal-type",
    "created_by": None, "shared": None, "parent": "food", "substitute": "food", "inherit_fields": None,
}

# Fields sent when recreating a deleted object (everything else is either
# read-only or would drag stale ids along). Unknown endpoints send all fields.
RECREATE_FIELDS = {
    "food": ["name", "plural_name", "description", "supermarket_category", "properties",
             "properties_food_amount", "properties_food_unit", "url", "fdc_id", "ignore_shopping"],
    "unit": ["name", "plural_name", "description", "base_unit"],
    "keyword": ["name", "description", "icon"],
}


# ---------- recording ----------

def current():
    return getattr(_local, "journal", None)


@contextlib.contextmanager
def recording():
    """Records the inverse of every Tandoor write in this thread into the
    yielded list (see RecordingClient)."""
    previous = current()
    journal: list[dict] = []
    _local.journal = journal
    try:
        yield journal
    finally:
        _local.journal = previous


def before_write(client, method: str, path: str, payload) -> dict | None:
    """Called by RecordingClient before a write; returns the pending entry
    (completed by after_write) or None when it can't be undone."""
    method = method.upper()
    move = _MOVE_PATH.match(path)
    if method == "PUT" and move:
        endpoint, obj_id = move.group(1), int(move.group(2))
        resp = client.get(f"/{endpoint}/{obj_id}/")
        if resp.status_code != 200:
            return None
        parent = resp.json().get("parent")
        parent = parent.get("id") if isinstance(parent, dict) else parent
        return {"op": "move", "endpoint": endpoint, "id": obj_id, "parent": parent}
    item = _ITEM_PATH.match(path)
    if method in ("PATCH", "PUT", "DELETE") and item:
        endpoint, obj_id = item.group(1), int(item.group(2))
        resp = client.get(path)
        if resp.status_code != 200:
            return None  # nothing there to restore (e.g. already deleted)
        snapshot = resp.json()
        if method == "DELETE":
            return {"op": "recreate", "endpoint": endpoint, "id": obj_id, "data": snapshot}
        keys = payload.keys() if isinstance(payload, dict) else snapshot.keys()
        return {"op": "restore", "endpoint": endpoint, "id": obj_id,
                "data": {k: snapshot[k] for k in keys if k in snapshot}}
    if method == "POST" and _LIST_PATH.match(path):
        return {"op": "delete", "endpoint": _LIST_PATH.match(path).group(1)}
    return None


def after_write(entry: dict | None, response) -> None:
    journal = current()
    if entry is None or journal is None or response.status_code >= 400:
        return
    if entry["op"] == "delete":
        try:
            new_id = response.json().get("id")
        except (ValueError, AttributeError):
            new_id = None
        if new_id is None:
            return
        entry["id"] = new_id
    journal.append(entry)


# ---------- storage ----------

def _dir() -> str:
    return os.path.join(settings.data_dir, "undo")


def _path(job_id: str, suggestion_id: str) -> str:
    return os.path.join(_dir(), f"{job_id}__{suggestion_id}.json")


def save(job_id: str, suggestion_id: str, journal: list[dict]) -> None:
    os.makedirs(_dir(), exist_ok=True)
    tmp = _path(job_id, suggestion_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(journal, f, ensure_ascii=False)
    os.replace(tmp, _path(job_id, suggestion_id))


def load(job_id: str, suggestion_id: str) -> list[dict] | None:
    try:
        with open(_path(job_id, suggestion_id), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def exists(job_id: str, suggestion_id: str) -> bool:
    return os.path.exists(_path(job_id, suggestion_id))


def discard(job_id: str, suggestion_id: str | None = None) -> None:
    """Removes one journal, or every journal of a run (suggestion_id None)."""
    if suggestion_id:
        with contextlib.suppress(OSError):
            os.remove(_path(job_id, suggestion_id))
        return
    with contextlib.suppress(OSError):
        for name in os.listdir(_dir()):
            if name.startswith(f"{job_id}__"):
                os.remove(os.path.join(_dir(), name))


# ---------- replay ----------

def _strip_ids(value, keep=False):
    """Copy without "id" keys - except inside references (REF_KEYS), which
    must keep pointing at their object."""
    if isinstance(value, list):
        return [_strip_ids(v, keep) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "id" and not keep:
                continue
            out[k] = _strip_ids(v, keep=k in REF_KEYS)
        return out
    return value


def _remap(value, remap, endpoint=None):
    """Rewrites ids of recreated objects in references."""
    if isinstance(value, list):
        return [_remap(v, remap, endpoint) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "id" and endpoint and (endpoint, v) in remap:
                out[k] = remap[(endpoint, v)]
            elif k in REF_KEYS:
                out[k] = _remap(v, remap, REF_KEYS[k])
            else:
                out[k] = _remap(v, remap, None)
        return out
    return value


def revert(client, job_id: str, suggestion_id: str) -> None:
    """Replays the saved journal backwards. Raises RuntimeError on the first
    step that fails (the steps done so far are dropped from the journal)."""
    journal = load(job_id, suggestion_id)
    if journal is None:
        raise RuntimeError("Nothing to undo (the undo data has expired).")
    remap: dict[tuple[str, int], int] = {}
    while journal:
        entry = journal[-1]
        endpoint = entry["endpoint"]
        obj_id = remap.get((endpoint, entry["id"]), entry["id"])
        if entry["op"] == "delete":
            if _still_used(client, endpoint, obj_id):
                journal.pop()  # e.g. a tag created back then that other recipes use by now
                save(job_id, suggestion_id, journal)
                continue
            resp = client.delete(f"/{endpoint}/{obj_id}/")
            if resp.status_code not in (200, 202, 204, 404):
                raise RuntimeError(f"Could not delete {endpoint} #{obj_id}: {resp.status_code} {resp.text[:200]}")
        elif entry["op"] == "recreate":
            fields = RECREATE_FIELDS.get(endpoint)
            data = {k: v for k, v in entry["data"].items() if fields is None or k in fields}
            payload = _remap(_strip_ids(data), remap)
            resp = client.post(f"/{endpoint}/", json=payload)
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"Could not recreate {endpoint} {entry['data'].get('name', '')!r}: "
                                   f"{resp.status_code} {resp.text[:200]}")
            remap[(endpoint, entry["id"])] = resp.json()["id"]
            # Later entries (earlier in time) may still refer to the old id.
            for other in journal[:-1]:
                if other["endpoint"] == endpoint and other["id"] == entry["id"]:
                    other["id"] = remap[(endpoint, entry["id"])]
        elif entry["op"] == "move":
            parent = entry.get("parent")
            parent = remap.get((endpoint, parent), parent) if parent else 0
            resp = client.put(f"/{endpoint}/{obj_id}/move/{parent}/")
            if resp.status_code >= 400 and resp.status_code != 404:
                raise RuntimeError(f"Could not move {endpoint} #{obj_id} back: {resp.status_code} {resp.text[:200]}")
        else:  # restore
            payload = _remap(entry["data"], remap)
            resp = client.patch(f"/{endpoint}/{obj_id}/", json=payload)
            if resp.status_code >= 400:
                # Nested objects (steps, ingredients) that no longer exist
                # can't be updated by id - send them as new copies instead.
                resp = client.patch(f"/{endpoint}/{obj_id}/", json=_remap(_strip_ids(entry["data"], keep=True), remap))
            if resp.status_code >= 400:
                raise RuntimeError(f"Could not restore {endpoint} #{obj_id}: {resp.status_code} {resp.text[:200]}")
        journal.pop()
        # Remaining references to recreated objects are rewritten in the
        # saved journal too, so a retry after a failure still works.
        journal[:] = [_remap_entry(e, remap) for e in journal]
        save(job_id, suggestion_id, journal)
    discard(job_id, suggestion_id)


USAGE_FILTERS = {"food": "foods", "keyword": "keywords"}


def _still_used(client, endpoint, obj_id) -> bool:
    """A food/tag created when the suggestion was applied is only deleted
    again if no recipe uses it (the restores before this step already took
    it out of the recipes of this change)."""
    param = USAGE_FILTERS.get(endpoint)
    if not param:
        return False
    resp = client.get("/recipe/", params={param: obj_id, "page_size": 1})
    if resp.status_code != 200:
        return True  # unsure - rather keep it
    data = resp.json()
    results = data.get("results", data) if isinstance(data, dict) else data
    return bool(data.get("count") if isinstance(data, dict) and "count" in data else results)


def _remap_entry(entry, remap):
    if entry["op"] in ("restore", "recreate"):
        return {**entry, "data": _remap(entry["data"], remap)}
    return entry


def cleanup(keep_job_ids: set[str]) -> None:
    """Removes journals of runs that no longer exist or are too old."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    with contextlib.suppress(OSError):
        for name in os.listdir(_dir()):
            path = os.path.join(_dir(), name)
            if name.split("__")[0] not in keep_job_ids or os.path.getmtime(path) < cutoff:
                with contextlib.suppress(OSError):
                    os.remove(path)
