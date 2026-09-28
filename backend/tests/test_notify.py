"""Push notifications via ntfy / Telegram (sending is faked)."""
import pytest

from app import notify, tool_jobs, usage_log
from app.config import settings
from app.schemas import Job, ToolSuggestion


@pytest.fixture
def sent(monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        return Resp()
    monkeypatch.setattr(notify.httpx, "post", fake_post)
    # send in the foreground so the test can look at it
    monkeypatch.setattr(notify, "_background", lambda fn, *args: fn(*args))
    monkeypatch.setattr(settings, "notify_ntfy_url", "https://ntfy.sh/meine-kueche")
    monkeypatch.setattr(settings, "notify_telegram_token", "123:abc")
    monkeypatch.setattr(settings, "notify_telegram_chat_id", "42")
    monkeypatch.setattr(settings, "app_url", "https://helper.example")
    return calls


def test_nothing_without_configuration(monkeypatch):
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("sent")))
    notify.send("test")
    notify.suggestions_ready(3)
    assert notify.test()["sent"] == []


def test_import_ready_goes_to_both_channels(sent):
    job = Job(id="j1", filename="Omas Karten", status="ready", source="folder")
    notify.import_finished(job)
    (ntfy_url, ntfy, _), (tg_url, tg, _) = sent
    assert ntfy_url == "https://ntfy.sh" and ntfy["topic"] == "meine-kueche"
    assert ntfy["title"] == "Tandoor Helper: Import fertig" and "Omas Karten: 0 Rezept(e)" in ntfy["message"]
    assert ntfy["click"] == "https://helper.example"
    assert tg_url == "https://api.telegram.org/bot123:abc/sendMessage" and tg["chat_id"] == "42"
    sent.clear()
    notify.import_finished(Job(id="j2", filename="x", status="ready"))  # imported at the screen: no message
    assert sent == []


def test_automatic_runs_are_collected(sent, monkeypatch):
    for n in (3, 4):
        job = tool_jobs.create_tool_job("conversions")
        job.meta["auto"] = True
        job.status = "ready"
        job.suggestions = [ToolSuggestion(id=f"s{i}", kind="x", summary="x") for i in range(n)]
        tool_jobs.save_tool_job(job)
        tool_jobs.save_tool_job(job)  # saved again: not counted twice
    assert sent == []  # still collecting
    notify._batch["timer"].cancel()
    notify._flush()
    assert len(sent) == 2 and "7 Vorschlag" in sent[0][1]["message"]


def test_budget_once_per_level_and_month(sent, monkeypatch):
    status = {"limit": 1000, "used": 850, "warn": True, "exceeded": False}
    notify.budget_changed(status)
    notify.budget_changed(status)
    notify.budget_changed({**status, "used": 1000, "exceeded": True})
    titles = [c[1]["title"] for c in sent if "topic" in (c[1] or {})]
    assert titles == ["Tandoor Helper: KI-Budget fast verbraucht", "Tandoor Helper: KI-Budget aufgebraucht"]


def test_recording_usage_checks_the_budget(monkeypatch):
    seen = []
    monkeypatch.setattr(notify, "budget_changed", seen.append)
    usage_log.record("import", 10, 5)
    assert seen and "used" in seen[0]


def test_errors_never_show_the_bot_token(sent, monkeypatch):
    def broken(url, **kw):
        raise notify.httpx.ConnectError(f"cannot reach {url}")
    monkeypatch.setattr(notify.httpx, "post", broken)
    result = notify.test()
    assert result["sent"] == [] and len(result["errors"]) == 2
    assert all("123:abc" not in e for e in result["errors"])
