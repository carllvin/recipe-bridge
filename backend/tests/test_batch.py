"""WATCH_BATCH: a cookbook's chunks as one AI batch (half price)."""
from types import SimpleNamespace

import anthropic

from app import ai_extractor, llm_provider
from app.config import settings

RECIPE = '[{"title": "Suppe", "ingredients": [{"name": "Wasser", "amount": 1, "unit": "l", "step_index": 0}], ' \
         '"steps": [{"instruction": "Kochen."}], "source_page_start": 1, "source_page_end": 1}]'


class FakeBatches:
    def __init__(self, fail=False):
        self.created, self.polls, self.fail = [], 0, fail

    def create(self, requests):
        self.created = requests
        return SimpleNamespace(id="b1")

    def retrieve(self, batch_id):
        self.polls += 1
        n = len(self.created)
        ended = self.polls >= 2
        return SimpleNamespace(processing_status="ended" if ended else "in_progress",
                               request_counts=SimpleNamespace(succeeded=n if ended else 0, errored=0, canceled=0, expired=0))

    def results(self, batch_id):
        for req in self.created:
            if self.fail:
                yield SimpleNamespace(custom_id=req["custom_id"], result=SimpleNamespace(type="errored"))
                continue
            message = SimpleNamespace(content=[SimpleNamespace(type="text", text=RECIPE)],
                                      usage=SimpleNamespace(input_tokens=1000, output_tokens=400,
                                                            cache_creation_input_tokens=0, cache_read_input_tokens=0))
            yield SimpleNamespace(custom_id=req["custom_id"], result=SimpleNamespace(type="succeeded", message=message))


def fake_claude(monkeypatch, batches, direct):
    class Client:
        def __init__(self, api_key=None):
            self.messages = SimpleNamespace(batches=batches, create=self.create)

        def create(self, **kw):
            direct.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="[]")],
                                   usage=SimpleNamespace(input_tokens=1000, output_tokens=10))
    monkeypatch.setattr(anthropic, "Anthropic", Client)
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "x")
    monkeypatch.setattr(llm_provider, "BATCH_POLL_SECONDS", 0)


PAGES = [{"page": n, "text": "Text " * 200} for n in range(1, 31)]


def test_cookbook_as_one_batch(monkeypatch):
    batches, direct, waits = FakeBatches(), [], []
    fake_claude(monkeypatch, batches, direct)
    recipes, usage = ai_extractor.extract_recipes_from_pages(PAGES, batch=True, on_wait=lambda d, t: waits.append((d, t)))
    assert len(batches.created) == 3 and not direct  # no single calls
    assert batches.created[0]["params"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert waits[-1] == (3, 3) and [r.title for r in recipes] == ["Suppe"]
    assert usage.input_tokens == 1500 and usage.output_tokens == 600  # half price


def test_failed_batch_falls_back_to_single_calls(monkeypatch):
    batches, direct = FakeBatches(fail=True), []
    fake_claude(monkeypatch, batches, direct)
    ai_extractor.extract_recipes_from_pages(PAGES, batch=True)
    assert len(direct) == 3


def test_without_batch_one_call_per_chunk(monkeypatch):
    batches, direct = FakeBatches(), []
    fake_claude(monkeypatch, batches, direct)
    ai_extractor.extract_recipes_from_pages(PAGES)
    assert len(direct) == 3 and not batches.created


def test_openai_batch(monkeypatch):
    import json
    state = {}

    class Files:
        def create(self, file, purpose):
            state["lines"] = [json.loads(line) for line in file[1].decode().splitlines()]
            return SimpleNamespace(id="f1")

        def content(self, file_id):
            out = [json.dumps({"custom_id": row["custom_id"], "response": {"status_code": 200, "body": {
                "choices": [{"message": {"content": RECIPE}}], "usage": {"prompt_tokens": 800, "completion_tokens": 200}}}})
                for row in state["lines"]]
            return SimpleNamespace(text="\n".join(reversed(out)))  # any order

    class Batches:
        def create(self, **kw):
            return SimpleNamespace(id="b1")

        def retrieve(self, batch_id):
            n = len(state["lines"])
            return SimpleNamespace(status="completed", output_file_id="o1",
                                   request_counts=SimpleNamespace(completed=n, failed=0, total=n))
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "x")
    monkeypatch.setattr(llm_provider, "_openai_client", lambda p: SimpleNamespace(files=Files(), batches=Batches()))
    out = llm_provider.complete_batch("sys", ["a", "b"], max_tokens=100)
    assert [t for t, _u in out] == [RECIPE, RECIPE] and out[0][1].input_tokens == 400
    assert state["lines"][0]["body"]["messages"][0] == {"role": "system", "content": "sys"}


def test_gemini_batch(monkeypatch):
    from google import genai
    state = {"polls": 0}

    class Batches:
        def create(self, model, src, config=None):
            state["src"], state["model"] = src, model
            return SimpleNamespace(name="batches/1")

        def get(self, name):
            state["polls"] += 1
            n, ended = len(state["src"]), state["polls"] >= 2
            response = SimpleNamespace(text=RECIPE, usage_metadata=SimpleNamespace(prompt_token_count=600,
                                                                                   candidates_token_count=100))
            return SimpleNamespace(
                state=SimpleNamespace(name="JOB_STATE_SUCCEEDED" if ended else "JOB_STATE_RUNNING"),
                completion_stats=SimpleNamespace(successful_count=n if ended else 0, failed_count=0),
                dest=SimpleNamespace(inlined_responses=[SimpleNamespace(response=response, error=None)] * n)
                if ended else None)
    monkeypatch.setattr(genai, "Client", lambda api_key=None: SimpleNamespace(batches=Batches()))
    monkeypatch.setattr(settings, "ai_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "x")
    monkeypatch.setattr(llm_provider, "BATCH_POLL_SECONDS", 0)
    assert llm_provider.batch_supported()
    waits = []
    out = llm_provider.complete_batch("sys", ["a", "b"], max_tokens=100, on_wait=lambda d, t: waits.append((d, t)))
    assert [t for t, _u in out] == [RECIPE, RECIPE] and out[0][1].input_tokens == 300 and out[0][1].output_tokens == 50
    assert state["src"][0]["config"] == {"max_output_tokens": 100, "system_instruction": "sys"}
    assert state["src"][1]["contents"][0]["parts"][0]["text"] == "b"
    assert waits == [(0, 2), (2, 2)]


def test_gemini_batch_too_large_asks_one_by_one(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "x")
    monkeypatch.setattr(llm_provider, "GEMINI_INLINE_MAX", 10)
    import pytest
    with pytest.raises(RuntimeError):
        llm_provider.submit_batch("sys", ["a" * 20])


def test_which_imports_go_as_batch(monkeypatch):
    from app import jobs, main
    monkeypatch.setattr(llm_provider, "batch_supported", lambda: True)
    job = jobs.create_job("buch.pdf")
    assert not main._as_batch(job)
    job.batch = True  # the import page's checkbox
    assert main._as_batch(job)
    folder = jobs.create_job("x.pdf")
    folder.source = "folder"
    assert main._as_batch(folder)  # WATCH_BATCH on by default
    monkeypatch.setattr(settings, "watch_batch", False)
    assert not main._as_batch(folder)
    monkeypatch.setattr(llm_provider, "batch_supported", lambda: False)
    assert not main._as_batch(job)  # e.g. a local model: as usual


def test_checkbox_reaches_the_job(monkeypatch):
    from fastapi.testclient import TestClient
    from app import jobs, main
    monkeypatch.setattr(main, "_run_extraction", lambda *a, **k: None)
    api = TestClient(main.app)
    jid = api.post("/api/import-url", json={"url": "https://a.example/r", "batch": True}).json()["job_id"]
    assert jobs.get_job(jid).batch
    jid = api.post("/api/import-links", json={"urls": ["https://a.example/r", "https://b.example/s"],
                                              "batch": True}).json()["job_id"]
    assert jobs.get_job(jid).batch
    jid = api.post("/api/upload", files=[("files", ("buch.txt", b"Ein Rezept mit genug Text.", "text/plain"))],
                   data={"batch": "true"}).json()["job_id"]
    assert jobs.get_job(jid).batch


def test_link_list_as_one_batch(monkeypatch):
    from app import jobs, main
    from app.schemas import ExtractedRecipe, TokenUsage
    monkeypatch.setattr(llm_provider, "batch_supported", lambda: True)
    monkeypatch.setattr(main, "process_url", lambda url, d: {"pages": [{"page": 1, "text": f"Recipe at {url}"}],
                                                            "images": {}, "page_count": 1})
    monkeypatch.setattr(main, "_fetch_existing_tags", lambda: [])
    monkeypatch.setattr(main, "_mark_duplicates", lambda job: None)
    monkeypatch.setattr(main.import_matching, "match_job_ingredients", lambda job: None)
    monkeypatch.setattr(main, "extract_recipes_from_pages", lambda *a, **k: (_ for _ in ()).throw(AssertionError("one by one")))
    seen = {}

    def batch(page_sets, existing_tags=None, on_wait=None, on_submit=None, batch_ref=None):
        seen["n"] = len(page_sets)
        return [([ExtractedRecipe(id=f"r{n}", title=f"R{n}", source_page_start=1, source_page_end=1)],
                 TokenUsage(input_tokens=50)) for n in range(len(page_sets))]
    monkeypatch.setattr(main, "extract_each_as_batch", batch)
    job = jobs.create_job("links")
    job.batch = True
    main._run_link_list(job, ["https://a.example/1", "https://b.example/2"], "/tmp")
    assert seen["n"] == 2 and [r.source_url for r in job.recipes] == ["https://a.example/1", "https://b.example/2"]
    assert job.token_usage.input_tokens == 100


def test_each_page_its_own_request_in_one_batch(monkeypatch):
    batches, direct = FakeBatches(), []
    fake_claude(monkeypatch, batches, direct)
    out = ai_extractor.extract_each_as_batch([[{"page": 1, "text": "Soup " * 50}], [{"page": 2, "text": "Cake " * 50}]])
    assert len(batches.created) == 2 and not direct
    assert all("system" in r["params"] for r in batches.created)
    assert [[r.title for r in recipes] for recipes, _u in out] == [["Suppe"], ["Suppe"]]


def _wait_for(job_id, status="ready"):
    import time
    from app import jobs
    for _ in range(200):
        job = jobs.get_job(job_id)
        if job and job.status in (status, "error"):
            return job
        time.sleep(0.02)
    return jobs.get_job(job_id)


def _quiet_finish(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_fetch_existing_tags", lambda: [])
    monkeypatch.setattr(main, "_mark_duplicates", lambda job: None)
    monkeypatch.setattr(main.import_matching, "match_job_ingredients", lambda job: None)
    monkeypatch.setattr(main, "guess_cookbook_title", lambda *a, **k: ("Buch", ai_extractor.TokenUsage()))


def test_batch_survives_a_restart(monkeypatch):
    """Sent, then the container restarts: the batch is collected afterwards, not sent again."""
    import os
    from app import jobs, main
    _quiet_finish(monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "x")
    monkeypatch.setattr(llm_provider, "batch_supported", lambda: True)
    sent = []
    monkeypatch.setattr(llm_provider, "submit_batch", lambda *a, **k: sent.append(1) or {"provider": "anthropic", "id": "b9", "n": 3})

    class Restart(Exception):
        pass

    def gone(ref, on_wait=None):
        raise Restart()
    monkeypatch.setattr(llm_provider, "collect_batch", gone)
    job = jobs.create_job("buch.pdf")
    job.batch = True
    monkeypatch.setattr(main, "process_pdf", lambda path, d, force_ocr=False: {
        "pages": PAGES, "images": {}, "page_count": len(PAGES), "metadata_title": "Buch"})
    main._run_extraction(job.id, ["/nowhere.pdf"], "pdf")  # the batch "fails" - falls back to single calls...
    assert sent == [1]
    # ...so simulate the real case: the container stopped while waiting - the file is still there
    main._remember_batch(job, {"provider": "anthropic", "id": "b9", "n": 3}, kind="pages", doc_type="pdf",
                         source_paths=["/nowhere.pdf"], result=main._keepable(
                             {"pages": PAGES, "images": {}, "page_count": len(PAGES)}))
    jobs._jobs.clear()
    collected = []

    def answers(ref, on_wait=None):
        collected.append(ref["id"])
        return [(RECIPE, ai_extractor.TokenUsage(input_tokens=500, output_tokens=100))] * ref["n"]
    monkeypatch.setattr(llm_provider, "collect_batch", answers)
    assert main._resume_batches() == 1
    resumed = _wait_for(job.id)
    assert collected == ["b9"] and sent == [1]  # nothing sent twice
    assert resumed.status == "ready" and [r.title for r in resumed.recipes] == ["Suppe"]
    import time
    for _ in range(100):
        if not os.path.exists(main._batch_path(job.id)):
            break
        time.sleep(0.02)
    assert not os.path.exists(main._batch_path(job.id))


def test_link_list_batch_survives_a_restart(monkeypatch):
    import os
    from app import jobs, main
    _quiet_finish(monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "x")
    monkeypatch.setattr(llm_provider, "batch_supported", lambda: True)
    job = jobs.create_job("2 links")
    job.batch = True
    loaded = [(1, "https://a.example/1", {"pages": [{"page": 1, "text": "Soup " * 50}], "images": {}}),
              (2, "https://b.example/2", {"pages": [{"page": 1, "text": "Cake " * 50}], "images": {}})]
    main._remember_batch(job, {"provider": "anthropic", "id": "b7", "n": 2}, kind="links", loaded=loaded)
    jobs._jobs.clear()
    monkeypatch.setattr(main, "process_url", lambda *a: (_ for _ in ()).throw(AssertionError("pages read again")))
    monkeypatch.setattr(llm_provider, "collect_batch",
                        lambda ref, on_wait=None: [(RECIPE, ai_extractor.TokenUsage(input_tokens=10))] * ref["n"])
    assert main._resume_batches() == 1
    resumed = _wait_for(job.id)
    assert resumed.status == "ready", resumed.error
    assert [r.source_url for r in resumed.recipes] == ["https://a.example/1", "https://b.example/2"]
    import time
    for _ in range(100):  # removed right after the job is done
        if not os.path.exists(main._batch_path(job.id)):
            break
        time.sleep(0.02)
    assert not os.path.exists(main._batch_path(job.id))
