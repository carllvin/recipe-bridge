"""Watched folder: PDFs, photos and documents put into WATCH_DIR (e.g. by a
scanner, a phone sync app or a network share) are imported on their own and
wait in the review inbox ("New imports").

- each file is its own import; a subfolder of photos is one photo import
  (e.g. the pages of one recipe),
- a file is only picked up once its size hasn't changed since the previous
  look, so half-copied files are left alone,
- afterwards the original moves to processed/ (or failed/, when it can't be
  imported), so nothing is imported twice,
- hidden files and typical partial downloads are skipped,
- nothing starts while the monthly AI budget is used up."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time

from . import usage_log
from .config import settings
from .image_processor import SUPPORTED_IMAGE_EXTENSIONS

log = logging.getLogger("tandoor-helper")

PROCESSED_DIR = "processed"
FAILED_DIR = "failed"
PARTIAL_SUFFIXES = (".part", ".partial", ".tmp", ".crdownload", ".download", ".filepart")

# path -> (size, mtime) seen in the previous look
_last_seen: dict[str, tuple[int, float]] = {}
_budget_logged = False


def _skip(name: str) -> bool:
    low = name.lower()
    return name.startswith((".", "~")) or low.endswith(PARTIAL_SUFFIXES) or name in (PROCESSED_DIR, FAILED_DIR)


def _signature(path: str) -> tuple[int, float] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime


def _candidates(watch_dir: str) -> list[tuple[str, list[str]]]:
    """(entry path, files) for every file / photo subfolder in the folder."""
    out = []
    try:
        names = sorted(os.listdir(watch_dir))
    except OSError:
        return out
    for name in names:
        if _skip(name):
            continue
        path = os.path.join(watch_dir, name)
        if os.path.isfile(path):
            out.append((path, [path]))
        elif os.path.isdir(path):
            files = sorted(
                os.path.join(path, f) for f in os.listdir(path)
                if not _skip(f) and os.path.isfile(os.path.join(path, f))
            )
            photos = [f for f in files if os.path.splitext(f)[1].lower() in SUPPORTED_IMAGE_EXTENSIONS]
            if photos:
                out.append((path, photos))
    return out


def _move(path: str, watch_dir: str, target: str) -> None:
    dest_dir = os.path.join(watch_dir, target)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{time.strftime('%Y%m%d-%H%M%S')}_{os.path.basename(path)}")
    shutil.move(path, dest)


def scan_once(watch_dir: str, start_import) -> list[str]:
    """One look into the folder. start_import(files, name) starts the import
    (copying the files) and returns the job id, or raises ValueError when the
    files can't be imported. Returns the started job ids."""
    global _budget_logged
    started = []
    seen_now: dict[str, tuple[int, float]] = {}
    for entry, files in _candidates(watch_dir):
        sigs = [_signature(f) for f in files]
        if any(s is None or s[0] == 0 for s in sigs):
            continue
        signature = (sum(s[0] for s in sigs), max(s[1] for s in sigs) + len(files))
        seen_now[entry] = signature
        if _last_seen.get(entry) != signature:
            continue  # new or still growing - look again next time
        if not usage_log.automatic_runs_allowed():
            if not _budget_logged:
                log.info("Watched folder: the monthly AI budget is used up - waiting")
                _budget_logged = True
            break
        _budget_logged = False
        try:
            started.append(start_import(files, os.path.basename(entry)))
            _move(entry, watch_dir, PROCESSED_DIR)
            log.info("Watched folder: importing %s", os.path.basename(entry))
        except ValueError as exc:
            log.warning("Watched folder: can't import %s (%s) - moved to %s/", os.path.basename(entry), exc,
                        FAILED_DIR)
            try:
                _move(entry, watch_dir, FAILED_DIR)
            except OSError:
                log.exception("Watched folder: could not move %s", entry)
        except OSError:
            log.exception("Watched folder: could not import %s", entry)
        seen_now.pop(entry, None)
    _last_seen.clear()
    _last_seen.update(seen_now)
    return started


async def loop(start_import) -> None:
    watch_dir = settings.watch_dir
    if not os.path.isdir(watch_dir):
        log.warning("WATCH_DIR %s is not a folder - the watched folder is off", watch_dir)
        return
    log.info("Watching %s for new recipes (every %ss)", watch_dir, settings.watch_interval_seconds)
    while True:
        try:
            await asyncio.to_thread(scan_once, watch_dir, start_import)
        except Exception:  # noqa: BLE001
            log.exception("Watched folder scan failed")
        await asyncio.sleep(max(5, settings.watch_interval_seconds))
