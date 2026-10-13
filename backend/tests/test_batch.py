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


def test_which_imports_go_as_batch(monkeypatch):
    from app import jobs, main
    job = jobs.create_job("buch.pdf")
    assert not main._as_batch(job, "pdf")
    monkeypatch.setattr(settings, "upload_batch", True)
    assert main._as_batch(job, "pdf") and not main._as_batch(job, "url")
    job.source = "folder"
    assert not main._as_batch(job, "pdf")  # the watched folder has its own switch
    monkeypatch.setattr(settings, "watch_batch", True)
    assert main._as_batch(job, "pdf")
    other = jobs.create_job("x.pdf")
    monkeypatch.setattr(settings, "upload_batch", False)
    other.batch = True  # chosen at the estimate
    assert main._as_batch(other, "pdf")


def test_estimate_button_asks_for_a_batch(monkeypatch):
    import threading
    import time
    from fastapi.testclient import TestClient
    from app import jobs, main
    monkeypatch.setattr(settings, "import_confirm_tokens", 1000)
    monkeypatch.setattr(llm_provider, "batch_supported", lambda: True)
    job = jobs.create_job("big.pdf")
    thread = threading.Thread(target=lambda: main._go_ahead(job, PAGES, None, None))
    thread.start()
    for _ in range(100):
        if job.status == "confirm":
            break
        time.sleep(0.02)
    TestClient(main.app).post(f"/api/jobs/{job.id}/confirm", json={"start": True, "batch": True})
    thread.join(5)
    assert job.batch and job.status == "processing"
