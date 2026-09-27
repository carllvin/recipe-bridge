"""The background queue applies strictly in order and resumes after a restart."""
import importlib
import json
import os
import time

import pytest

from app import apply_queue as apply_queue_module
from app.config import settings


@pytest.fixture
def queue():
    # A fresh module per test: new queue, new worker thread (the old one
    # just keeps waiting on the old module's condition).
    return importlib.reload(apply_queue_module)


def wait_until(check, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


class Suggestion:
    def __init__(self, status, summary="", error=None):
        self.status, self.summary, self.error = status, summary, error


def test_processes_in_order_and_reports_failures(queue):
    done = []

    def perform(job_id, sid, action):
        done.append((sid, action))
        return Suggestion("error" if sid == "b" else "applied", summary=sid, error="kaputt" if sid == "b" else None)

    queue.configure(perform)
    queue.start()
    batch = queue.enqueue("apply", [{"job_id": "j", "id": x} for x in "abc"])
    assert wait_until(lambda: queue.status(batch)["batch"]["done"] == 3)
    status = queue.status(batch)["batch"]
    assert done == [("a", "apply"), ("b", "apply"), ("c", "apply")]
    assert status["failed"] == 1 and status["errors"][0]["error"] == "kaputt"
    assert queue.status()["queued"] == 0


def test_already_queued_items_are_not_added_twice(queue):
    queue.configure(lambda *a: time.sleep(0.2) or Suggestion("applied"))
    queue.start()
    first = queue.enqueue("apply", [{"job_id": "j", "id": "a"}, {"job_id": "j", "id": "b"}])
    second = queue.enqueue("skip", [{"job_id": "j", "id": "b"}, {"job_id": "j", "id": "c"}])
    assert queue.status(first)["batch"]["total"] == 2
    assert queue.status(second)["batch"]["total"] == 1


def test_unknown_action_is_rejected(queue):
    with pytest.raises(ValueError):
        queue.enqueue("delete-everything", [])


def test_resumes_saved_queue_after_restart(queue):
    saved = [{"batch": "b1", "job_id": "j", "id": x, "action": "apply"} for x in ("c4", "c5")]
    with open(os.path.join(settings.data_dir, "apply_queue.json"), "w") as f:
        json.dump(saved, f)
    done = []
    queue.configure(lambda job_id, sid, action: done.append(sid) or Suggestion("applied"))
    queue.start()
    assert wait_until(lambda: done == ["c4", "c5"])
    assert wait_until(lambda: json.load(open(os.path.join(settings.data_dir, "apply_queue.json"))) == [])
