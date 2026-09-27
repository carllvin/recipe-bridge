"""Phone share target (/share), the review inbox's "new imports" and the
watched folder."""
import os

import pytest
from fastapi.testclient import TestClient

from app import jobs, main, usage_log, watcher
from app.schemas import ExtractedRecipe


@pytest.fixture
def started(monkeypatch):
    """Records extractions instead of running them."""
    calls = []
    monkeypatch.setattr(main, "_run_extraction", lambda job_id, paths, doc_type, *a: calls.append((job_id, paths, doc_type)))
    with jobs._lock:
        jobs._jobs.clear()
    return calls


@pytest.fixture
def client():
    return TestClient(main.app)


def test_share_link_starts_url_import(client, started):
    resp = client.post("/share", data={"title": "Lasagne", "text": "Schau mal https://example.com/lasagne !"},
                       follow_redirects=False)
    assert resp.status_code == 303
    job_id = resp.headers["location"].split("job=")[1]
    assert started[0][1:] == (["https://example.com/lasagne"], "url")
    assert jobs.get_job(job_id).source == "share"


def test_share_text_and_files(client, started):
    text = "Pfannkuchen\n200 g Mehl\n2 Eier\nAlles verrühren und ausbacken."
    assert client.post("/share", data={"text": text}, follow_redirects=False).status_code == 303
    assert started[-1][2] == "text"
    resp = client.post("/share", files=[("files", ("seite.jpg", b"\xff\xd8fake", "image/jpeg"))],
                       follow_redirects=False)
    assert resp.status_code == 303 and started[-1][2] == "images"
    assert os.path.exists(started[-1][1][0])


def test_share_error_redirects_with_message(client, started):
    resp = client.post("/share", files=[("files", ("x.exe", b"MZ", "application/octet-stream"))],
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/?error=")
    assert not started


def test_inbox_lists_and_dismisses_new_imports(client, started):
    client.post("/share", data={"url": "https://example.com/a"})
    job = jobs.list_jobs()[0]
    data = client.get("/api/inbox").json()
    assert [i["status"] for i in data["imports"]] == ["processing"]
    assert client.get("/api/inbox/count").json()["count"] == 0  # still being read
    job.status = "ready"
    job.recipes = [ExtractedRecipe(id="r1", title="A", source_page_start=1, source_page_end=1)]
    assert client.get("/api/inbox").json()["imports"][0]["recipes"] == 1
    assert client.get("/api/inbox/count").json()["count"] == 1
    client.post(f"/api/jobs/{job.id}/dismiss")
    assert client.get("/api/inbox").json()["imports"] == []


def test_watcher_waits_for_stable_files_then_imports(tmp_path, started):
    watch = tmp_path / "import"
    watch.mkdir()
    (watch / "kochbuch.pdf").write_bytes(b"%PDF-1.4 fake")
    (watch / ".versteckt.pdf").write_bytes(b"x")
    (watch / "halb.pdf.part").write_bytes(b"x")
    album = watch / "Omas Kuchen"
    album.mkdir()
    (album / "1.jpg").write_bytes(b"\xff\xd8a")
    (album / "2.jpg").write_bytes(b"\xff\xd8b")
    (watch / "notiz.xyz").write_bytes(b"?")
    watcher._last_seen.clear()

    assert watcher.scan_once(str(watch), main._start_folder_import) == []  # first look: not yet stable
    ids = watcher.scan_once(str(watch), main._start_folder_import)
    assert len(ids) == 2
    kinds = sorted(d for _, _, d in started)
    assert kinds == ["images", "pdf"]
    photo_job = next(jobs.get_job(i) for i in ids if jobs.get_job(i).filename == "Omas Kuchen")
    assert photo_job.source == "folder"
    left = sorted(os.listdir(watch))
    assert "kochbuch.pdf" not in left and "Omas Kuchen" not in left
    assert len(os.listdir(watch / "processed")) == 2
    assert os.listdir(watch / "failed")[0].endswith("notiz.xyz")
    assert ".versteckt.pdf" in left and "halb.pdf.part" in left


def test_watcher_grows_and_budget(tmp_path, started, monkeypatch):
    watch = tmp_path / "w"
    watch.mkdir()
    f = watch / "a.pdf"
    f.write_bytes(b"%PDF")
    watcher._last_seen.clear()
    watcher.scan_once(str(watch), main._start_folder_import)
    f.write_bytes(b"%PDF more")  # still being copied
    assert watcher.scan_once(str(watch), main._start_folder_import) == []
    monkeypatch.setattr(usage_log, "automatic_runs_allowed", lambda: False)
    assert watcher.scan_once(str(watch), main._start_folder_import) == []
    assert f.exists()
    monkeypatch.setattr(usage_log, "automatic_runs_allowed", lambda: True)
    assert len(watcher.scan_once(str(watch), main._start_folder_import)) == 1
