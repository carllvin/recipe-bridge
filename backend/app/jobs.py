from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid

from .schemas import Job

log = logging.getLogger("recipe-bridge")

_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def create_job(filename: str) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], filename=filename)
    with _lock:
        _jobs[job.id] = job
    return job


def get_job(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def list_jobs() -> list[Job]:
    with _lock:
        return list(_jobs.values())


def save_job(job: Job) -> None:
    with _lock:
        _jobs[job.id] = job


def match_images_to_recipes(job: Job, page_margin: int = 1) -> None:
    """Assigns each recipe candidate images whose page falls within the (expanded) page range."""
    for recipe in job.recipes:
        start = recipe.source_page_start - page_margin
        end = recipe.source_page_end + page_margin
        candidates = [
            image_id
            for image_id, info in job.images.items()
            if start <= info["page"] <= end
        ]
        # Prefer images right in the core range, then the margin
        candidates.sort(
            key=lambda iid: abs(job.images[iid]["page"] - (recipe.source_page_start + recipe.source_page_end) / 2)
        )
        recipe.candidate_image_ids = candidates
        if candidates and not recipe.selected_image_id:
            recipe.selected_image_id = candidates[0]


def cleanup_old_jobs(data_dir: str, retention_hours: int) -> int:
    """Removes jobs (and their uploaded PDF/extracted images on disk) older than
    retention_hours. Returns the number of jobs removed. Safe to call repeatedly
    (e.g. from a periodic background task) - jobs still within the retention
    window are left untouched."""
    if retention_hours <= 0:
        return 0

    cutoff = time.time() - retention_hours * 3600
    removed = 0

    with _lock:
        stale_ids = [jid for jid, job in _jobs.items() if job.created_at < cutoff]
        for jid in stale_ids:
            del _jobs[jid]

    for jid in stale_ids:
        job_dir = os.path.join(data_dir, jid)
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not remove data directory for job %s: %s", jid, exc)
        removed += 1

    if removed:
        log.info("Cleanup: removed %d job(s) older than %d hour(s)", removed, retention_hours)

    return removed
