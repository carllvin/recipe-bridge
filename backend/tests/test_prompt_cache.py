"""Claude caches the extraction's instructions across a cookbook's chunks."""
from types import SimpleNamespace

import anthropic

from app import ai_extractor, llm_provider
from app.config import settings


class FakeClaude:
    calls = []

    def __init__(self, api_key=None):
        self.messages = self

    def create(self, **kw):
        FakeClaude.calls.append(kw)
        first = len(FakeClaude.calls) == 1
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200,
                                cache_creation_input_tokens=2000 if first else 0,
                                cache_read_input_tokens=0 if first else 2000)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="[]")], usage=usage)


def test_chunks_share_the_cached_instructions(monkeypatch):
    FakeClaude.calls = []
    monkeypatch.setattr(anthropic, "Anthropic", FakeClaude)
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "x")
    pages = [{"page": n, "text": "Text " * 200} for n in range(1, 31)]
    _recipes, usage = ai_extractor.extract_recipes_from_pages(pages)
    assert len(FakeClaude.calls) == 3
    system = FakeClaude.calls[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert all(c["system"] == system for c in FakeClaude.calls)  # byte-identical -> cache hits
    # billed: 3 x 1000 + 2000 written at 1.25 + 2 x 2000 read at a tenth
    assert usage.input_tokens == 3000 + 2500 + 400 and usage.output_tokens == 600


def test_single_calls_are_not_cached(monkeypatch):
    FakeClaude.calls = []
    monkeypatch.setattr(anthropic, "Anthropic", FakeClaude)
    monkeypatch.setattr(settings, "anthropic_api_key", "x")
    llm_provider.complete_text("instructions", "text")
    assert FakeClaude.calls[0]["system"] == "instructions"
