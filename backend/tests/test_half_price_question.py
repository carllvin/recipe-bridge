"""A large import without "half price" asks once more."""
import threading
import time

from fastapi.testclient import TestClient

from app import ai_extractor, jobs, llm_provider, main

PAGES = [{"page": n, "text": "Mix the flour with the water and knead. " * 60} for n in range(1, 61)]


def ask(job, estimate):
    answer = {}
    thread = threading.Thread(target=lambda: answer.setdefault("go", main._go_ahead(job, estimate)))
    thread.start()
    for _ in range(100):
        if job.status == "confirm":
            break
        time.sleep(0.02)
    return thread, answer


def test_large_estimate_waits_and_takes_half_price(monkeypatch):
    monkeypatch.setattr(llm_provider, "batch_supported", lambda: True)
    job = jobs.create_job("big.pdf")
    assert main._asks_about_half_price(job)
    estimate = ai_extractor.estimate_tokens(PAGES)
    assert estimate["total"] >= main.LARGE_IMPORT_TOKENS
    thread, answer = ask(job, estimate)
    assert job.status == "confirm" and TestClient(main.app).get(f"/api/jobs/{job.id}").json()["estimate"]["total"]
    TestClient(main.app).post(f"/api/jobs/{job.id}/confirm", json={"start": True, "batch": True})
    thread.join(5)
    assert answer["go"] is True and job.batch and main._as_batch(job)


def test_full_price_or_cancel(monkeypatch):
    monkeypatch.setattr(llm_provider, "batch_supported", lambda: True)
    api = TestClient(main.app)
    full = jobs.create_job("a.pdf")
    thread, answer = ask(full, {"total": 99999})
    api.post(f"/api/jobs/{full.id}/confirm", json={"start": True})
    thread.join(5)
    assert answer["go"] is True and not full.batch
    gone = jobs.create_job("b.pdf")
    thread, answer = ask(gone, {"total": 99999})
    api.post(f"/api/jobs/{gone.id}/confirm", json={"start": False})
    thread.join(5)
    assert answer["go"] is False and gone.status == "cancelled"
    assert gone.id not in [j["id"] for j in api.get("/api/jobs").json()]


def test_no_question_when_small_chosen_or_unattended(monkeypatch):
    monkeypatch.setattr(llm_provider, "batch_supported", lambda: True)
    assert main._go_ahead(jobs.create_job("small"), {"total": 1000}) is True
    chosen = jobs.create_job("c.pdf")
    chosen.batch = True
    assert not main._asks_about_half_price(chosen)
    folder = jobs.create_job("d.pdf")
    folder.source = "folder"
    assert not main._asks_about_half_price(folder)
    monkeypatch.setattr(llm_provider, "batch_supported", lambda: False)
    assert not main._asks_about_half_price(jobs.create_job("gemini"))  # no batches: nothing to offer


def test_long_link_list_asks(monkeypatch):
    monkeypatch.setattr(llm_provider, "batch_supported", lambda: True)
    monkeypatch.setattr(main, "_fetch_existing_tags", lambda: [])
    asked = []
    monkeypatch.setattr(main, "_go_ahead", lambda job, est: asked.append(est) or False)
    job = jobs.create_job("links")
    job.status = "cancelled"
    main._run_link_list(job, [f"https://a.example/{n}" for n in range(15)], "/tmp")
    assert asked and asked[0]["links"] == 15 and asked[0]["total"] == 15 * main.TOKENS_PER_LINK
