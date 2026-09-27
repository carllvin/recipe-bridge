"""Token usage over time: every finished tool run and cookbook/URL import
appends one line to usage.jsonl in the data volume, so the UI can show what
the AI actually used in the last 30 days (per tool)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from .config import settings

log = logging.getLogger("tandoor-helper")
_lock = threading.Lock()


def _path() -> str:
    return os.path.join(settings.data_dir, "usage.jsonl")


def record(source: str, input_tokens: int, output_tokens: int) -> None:
    if input_tokens <= 0 and output_tokens <= 0:
        return
    line = json.dumps({"ts": time.time(), "source": source, "in": int(input_tokens), "out": int(output_tokens)})
    try:
        with _lock:
            os.makedirs(settings.data_dir, exist_ok=True)
            with open(_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as exc:
        log.warning("Could not record token usage: %s", exc)


def summary(days: int = 30) -> dict:
    cutoff = time.time() - days * 86400
    by_source: dict[str, dict] = {}
    total = {"input_tokens": 0, "output_tokens": 0}
    try:
        with open(_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("ts", 0) < cutoff:
                    continue
                src = by_source.setdefault(entry.get("source", "?"), {"input_tokens": 0, "output_tokens": 0})
                for key, short in (("input_tokens", "in"), ("output_tokens", "out")):
                    src[key] += entry.get(short, 0)
                    total[key] += entry.get(short, 0)
    except FileNotFoundError:
        pass
    return {"days": days, "total": total, "by_source": by_source}


def month_total() -> int:
    """Input + output tokens used since the start of the current calendar
    month (local time)."""
    now = time.localtime()
    start = time.mktime((now.tm_year, now.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    total = 0
    try:
        with open(_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("ts", 0) >= start:
                    total += entry.get("in", 0) + entry.get("out", 0)
    except FileNotFoundError:
        pass
    return total


def budget_status() -> dict:
    """{"limit", "used", "exceeded", "warn", "block_manual"} for the monthly
    token budget from the UI settings (limit 0 = no budget)."""
    from . import app_settings  # late import: app_settings is independent of this module

    budget = app_settings.get()["budget"]
    limit, used = budget["monthly_tokens"], month_total()
    return {"limit": limit, "used": used, "exceeded": bool(limit) and used >= limit,
            "warn": bool(limit) and used >= 0.8 * limit, "block_manual": budget["block_manual"]}


def automatic_runs_allowed() -> bool:
    return not budget_status()["exceeded"]


def manual_runs_allowed() -> bool:
    status = budget_status()
    return not (status["exceeded"] and status["block_manual"])
