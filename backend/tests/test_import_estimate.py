"""A large import shows its estimated tokens and waits for the OK."""
import threading
import time

from fastapi.testclient import TestClient

from app import ai_extractor, jobs, main
from app.config import settings

PAGES = [{"page": n, "text": "Mix the flour with the water and knead. " * 50} for n in range(1, 31)]


def test_estimate_counts_overlap_and_instructions():
    est = ai_extractor.estimate_tokens(PAGES)
    text = sum(len(p["text"]) for p in PAGES) // 4
    assert est["chunks"] == 3 and est["input"] > text  # overlapping pages and the prompt per chunk
    assert est["total"] == est["input"] + est["output"] and 0 < est["output"] < est["input"]
    assert ai_extractor.estimate_tokens([{"page": 1, "text": " "}])["total"] == 0


def _waiting(monkeypatch, limit=1000):
    monkeypatch.setattr(settings, "import_confirm", "large")
    monkeypatch.setattr(settings, "import_confirm_tokens", limit)
    job = jobs.create_job("Big book.pdf")
    answer = {}
    thread = threading.Thread(target=lambda: answer.setdefault("go", main._go_ahead(job, PAGES, None, None)))
    thread.start()
    for _ in range(100):
        if job.status == "confirm":
            break
        time.sleep(0.02)
    return job, thread, answer


def test_large_import_waits_and_starts(monkeypatch):
    job, thread, answer = _waiting(monkeypatch)
    assert job.status == "confirm" and job.estimate["chunks"] == 3 and job.estimate["budget_left"] is None
    api = TestClient(main.app)
    assert api.get(f"/api/jobs/{job.id}").json()["estimate"]["total"] > 1000
    assert api.post(f"/api/jobs/{job.id}/confirm", json={"start": True}).json() == {"status": "processing"}
    thread.join(5)
    assert answer["go"] is True


def test_large_import_cancelled(monkeypatch):
    job, thread, answer = _waiting(monkeypatch)
    api = TestClient(main.app)
    api.post(f"/api/jobs/{job.id}/confirm", json={"start": False})
    thread.join(5)
    assert answer["go"] is False and job.status == "cancelled"
    assert job.id not in [j["id"] for j in api.get("/api/jobs").json()]
    assert api.post(f"/api/jobs/{job.id}/confirm", json={"start": True}).status_code == 409


def test_small_or_unattended_imports_run_on(monkeypatch):
    monkeypatch.setattr(settings, "import_confirm", "large")
    monkeypatch.setattr(settings, "import_confirm_tokens", 10**9)
    assert main._go_ahead(jobs.create_job("small"), PAGES, None, None) is True
    monkeypatch.setattr(settings, "import_confirm_tokens", 1000)
    shared = jobs.create_job("from the phone")
    shared.source = "share"
    assert main._go_ahead(shared, PAGES, None, None) is True and shared.status == "processing"
    monkeypatch.setattr(settings, "import_confirm", "never")
    assert main._go_ahead(jobs.create_job("off"), PAGES, None, None) is True


def test_always_asks_for_a_cookbook_not_for_a_link(monkeypatch):
    monkeypatch.setattr(settings, "import_confirm", "always")
    assert main._go_ahead(jobs.create_job("link"), PAGES[:1], None, None, "url") is True
    job = jobs.create_job("small.pdf")
    thread = threading.Thread(target=lambda: main._go_ahead(job, PAGES[:2], None, None, "pdf"))
    thread.start()
    for _ in range(100):
        if job.status == "confirm":
            break
        time.sleep(0.02)
    assert job.status == "confirm" and job.estimate["chunks"] == 1  # even a small one
    TestClient(main.app).post(f"/api/jobs/{job.id}/confirm", json={"start": False})
    thread.join(5)


def test_estimate_counts_the_cache_with_claude(monkeypatch):
    from app import llm_provider
    monkeypatch.setattr(llm_provider, "_active_provider", lambda: "anthropic")
    cached = ai_extractor.estimate_tokens(PAGES)
    monkeypatch.setattr(llm_provider, "_active_provider", lambda: "openai")
    assert cached["input"] < ai_extractor.estimate_tokens(PAGES)["input"]
