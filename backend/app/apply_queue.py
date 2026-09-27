"""Applies or skips reviewed suggestions in the background, so a bulk
"apply selected" keeps going when the page is closed.

One worker thread works through a single FIFO queue - strictly one after
another, across all runs: merges touch shared recipes, and doing them in
order keeps the same safety as clicking them one by one. The queue is
saved in the data volume, so a restart picks up where it stopped.

Items are grouped into batches (one per click) so the page can show
"12/40 done, 1 failed"; batch statistics live in memory only."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque

from .config import settings

log = logging.getLogger("tandoor-helper")

MAX_BATCHES = 50  # finished batches kept for status queries

_cond = threading.Condition()
_queue: deque = deque()        # {"batch", "job_id", "id", "action"}
_batches: dict[str, dict] = {}
_current: dict | None = None
_perform = None                # set by configure(): (job_id, id, action) -> suggestion
_started = False


def _path() -> str:
    return os.path.join(settings.data_dir, "apply_queue.json")


def _save() -> None:
    """Caller holds _cond. The item being worked on is saved too, so it is
    retried after a restart (applying is idempotent: a suggestion that is no
    longer pending is left alone)."""
    items = ([_current] if _current else []) + list(_queue)
    try:
        os.makedirs(settings.data_dir, exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f)
        os.replace(tmp, _path())
    except OSError as exc:
        log.warning("Could not save the apply queue: %s", exc)


def configure(perform) -> None:
    global _perform
    _perform = perform


def start() -> None:
    """Loads a saved queue and starts the worker (once)."""
    global _started
    with _cond:
        if _started:
            return
        _started = True
        try:
            with open(_path(), encoding="utf-8") as f:
                saved = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            saved = []
        for item in saved if isinstance(saved, list) else []:
            if isinstance(item, dict) and {"batch", "job_id", "id", "action"} <= item.keys():
                _queue.append(item)
                batch = _batches.setdefault(item["batch"], _new_batch(item["action"]))
                batch["total"] += 1
        if _queue:
            log.info("Resuming %d queued suggestion action(s)", len(_queue))
    threading.Thread(target=_worker, daemon=True).start()


def _new_batch(action) -> dict:
    return {"action": action, "total": 0, "done": 0, "failed": 0, "errors": [],
            "created_at": time.time(), "finished_at": None}


def enqueue(action: str, items: list[dict]) -> str:
    """items: [{"job_id", "id"}] in the order to process them. Items that
    are already queued are left out. Returns the batch id."""
    if action not in ("apply", "skip", "retry", "undo"):
        raise ValueError(f"Unknown action {action!r}")
    batch_id = uuid.uuid4().hex[:10]
    with _cond:
        queued = queued_keys_locked()
        batch = _new_batch(action)
        for item in items:
            key = (str(item.get("job_id")), str(item.get("id")))
            if key in queued or not all(key):
                continue
            queued.add(key)
            _queue.append({"batch": batch_id, "job_id": key[0], "id": key[1], "action": action})
            batch["total"] += 1
        if batch["total"] == 0:
            batch["finished_at"] = time.time()
        _batches[batch_id] = batch
        _trim_batches()
        _save()
        _cond.notify()
    return batch_id


def queued_keys_locked() -> set[tuple[str, str]]:
    keys = {(i["job_id"], i["id"]) for i in _queue}
    if _current:
        keys.add((_current["job_id"], _current["id"]))
    return keys


def queued_keys() -> set[tuple[str, str]]:
    with _cond:
        return queued_keys_locked()


def status(batch_id: str | None = None) -> dict:
    with _cond:
        result = {"queued": len(_queue) + (1 if _current else 0)}
        if batch_id:
            batch = _batches.get(batch_id)
            result["batch"] = dict(batch, errors=list(batch["errors"])) if batch else None
        return result


def _trim_batches() -> None:
    finished = sorted((b["finished_at"], bid) for bid, b in _batches.items() if b["finished_at"])
    for _, bid in finished[:max(0, len(finished) - MAX_BATCHES)]:
        _batches.pop(bid, None)


def _worker() -> None:
    global _current
    while True:
        with _cond:
            while not _queue:
                _cond.wait()
            _current = _queue.popleft()
            item = _current
        error, summary = None, None
        try:
            suggestion = _perform(item["job_id"], item["id"], item["action"])
            summary = getattr(suggestion, "summary", None)
            if getattr(suggestion, "status", None) == "error":
                error = suggestion.error or "error"
        except Exception as exc:  # noqa: BLE001
            log.warning("Queued %s of %s/%s failed: %s", item["action"], item["job_id"], item["id"], exc)
            error = str(exc)
        with _cond:
            _current = None
            batch = _batches.get(item["batch"])
            if batch:
                batch["done"] += 1
                if error:
                    batch["failed"] += 1
                    batch["errors"].append({"job_id": item["job_id"], "id": item["id"],
                                            "summary": summary, "error": error})
                if batch["done"] >= batch["total"]:
                    batch["finished_at"] = time.time()
            _save()
