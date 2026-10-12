"""Maintain -> Diagnosis: shows what the container uses, never a secret."""
from fastapi.testclient import TestClient

from app import diagnostics, llm_provider, main
from app.config import settings
from app.schemas import TokenUsage


def test_rows_without_secrets(tandoor, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-secret-key")
    monkeypatch.setattr(settings, "app_password", "")
    monkeypatch.setattr(settings, "translate_recipes", False)
    response = TestClient(main.app).get("/api/diagnostics")
    assert response.status_code == 200
    rows = {r["label"]: r for r in response.json()["rows"]}
    assert rows["TRANSLATE_RECIPES"]["value"] == "false"
    assert rows["Key / server"]["value"] == "set" and rows["Key / server"]["ok"] is True
    assert rows["Reachable"]["ok"] is True
    assert "sk-secret-key" not in response.text


def test_unreachable_manager_is_marked(monkeypatch):
    def broken():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(diagnostics.target, "client", broken)
    rows = {r["label"]: r for r in diagnostics.info()}
    assert rows["Reachable"]["ok"] is False and "connection refused" in rows["Reachable"]["value"]


def test_ai_test_without_key(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    result = TestClient(main.app).post("/api/diagnostics/ai-test").json()
    assert result["ok"] is False and "ANTHROPIC_API_KEY" in result["error"]


def test_ai_test_ok(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "x")
    monkeypatch.setattr(llm_provider, "complete_text",
                        lambda *a, **k: ("OK", TokenUsage(input_tokens=12, output_tokens=1)))
    result = diagnostics.ai_test()
    assert result["ok"] is True and result["reply"] == "OK" and result["model"] == settings.claude_model
