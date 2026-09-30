"""AI_PROVIDER=compatible / ollama: an OpenAI-compatible API (local models)."""
import types

import pytest

from app import llm_provider
from app.config import settings


@pytest.fixture
def fake_openai(monkeypatch):
    made = []

    class Completions:
        def create(self, **kw):
            made[-1]["calls"].append(kw)
            if "max_completion_tokens" in kw:
                raise ValueError("unknown parameter")  # like many local servers
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))],
                                         usage=types.SimpleNamespace(prompt_tokens=3, completion_tokens=1))

    class FakeOpenAI:
        def __init__(self, **kw):
            made.append({"init": kw, "calls": []})
            self.chat = types.SimpleNamespace(completions=Completions())
    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    return made


def configure(monkeypatch, provider="ollama", **extra):
    monkeypatch.setattr(settings, "ai_provider", provider)
    monkeypatch.setattr(settings, "compatible_base_url", "http://ollama:11434/v1")
    monkeypatch.setattr(settings, "compatible_model", "qwen2.5:14b")
    monkeypatch.setattr(settings, "compatible_tools_model", extra.get("tools", ""))
    monkeypatch.setattr(settings, "compatible_api_key", "")


def test_ollama_uses_the_compatible_server(monkeypatch, fake_openai):
    configure(monkeypatch)
    assert llm_provider.is_configured()
    text, usage = llm_provider.complete_text("sys", "hallo")
    assert text == "ok" and usage.input_tokens == 3
    init = fake_openai[-1]["init"]
    assert init["base_url"] == "http://ollama:11434/v1" and init["api_key"] == "not-needed"
    calls = fake_openai[-1]["calls"]
    assert calls[-1]["model"] == "qwen2.5:14b" and "max_tokens" in calls[-1]  # fell back from max_completion_tokens


def test_tools_model(monkeypatch, fake_openai):
    configure(monkeypatch, provider="compatible", tools="llama3.1:8b")
    llm_provider.complete_tool_text("sys", "x")
    assert fake_openai[-1]["calls"][-1]["model"] == "llama3.1:8b"


def test_not_configured_without_url_or_model(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(settings, "compatible_model", "")
    assert not llm_provider.is_configured()
    assert "COMPATIBLE_BASE_URL" in llm_provider.missing_key_hint()


def test_images_go_to_the_same_server(monkeypatch, fake_openai):
    configure(monkeypatch)
    text, _usage = llm_provider.transcribe_image(b"jpeg", "lies das")
    assert text == "ok" and fake_openai[-1]["init"]["base_url"] == "http://ollama:11434/v1"
    content = fake_openai[-1]["calls"][-1]["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
