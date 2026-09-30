"""Dictated recipes: a voice note is transcribed and read like a text."""
import types

import pytest

from app import jobs, llm_provider, main
from app.config import settings
from app.schemas import ExtractedRecipe, TokenUsage


def test_audio_uploads_are_voice_notes():
    assert main._classify_upload(["Sprachnotiz.m4a"]) == ("audio", "")
    assert main._classify_upload(["aufnahme.webm"]) == ("audio", "")
    assert main._classify_upload(["a.m4a", "b.m4a"])[0] == ""  # one at a time


def test_voice_note_runs_through_the_text_extraction(monkeypatch, tmp_path):
    heard, extracted = [], []

    def transcribe(data, filename):
        heard.append((data, filename))
        return "Omas Pfannkuchen: 250 Gramm Mehl, drei Eier, ein halber Liter Milch, alles verrühren.", TokenUsage(input_tokens=7)

    def extract(pages, existing_tags=None, **kw):
        extracted.append(pages[0]["text"])
        return [ExtractedRecipe(id="r1", title="Pfannkuchen", source_page_start=1, source_page_end=1)], TokenUsage(input_tokens=100)
    monkeypatch.setattr(llm_provider, "transcribe_audio", transcribe)
    monkeypatch.setattr(main, "extract_recipes_from_pages", extract)
    monkeypatch.setattr(main, "_fetch_existing_tags", lambda: [])
    monkeypatch.setattr(main, "_mark_duplicates", lambda job: None)
    monkeypatch.setattr(main.import_matching, "match_job_ingredients", lambda job: None)
    note = tmp_path / "source_000.m4a"
    note.write_bytes(b"audio")
    job = jobs.create_job("Sprachnotiz.m4a")
    main._run_extraction(job.id, [str(note)], "audio")
    job = jobs.get_job(job.id)
    assert job.status == "ready", job.error
    assert [r.title for r in job.recipes] == ["Pfannkuchen"]
    assert heard == [(b"audio", "source_000.m4a")] and "250 Gramm Mehl" in extracted[0]
    assert job.token_usage.input_tokens >= 107


@pytest.mark.parametrize("active,openai_key,gemini_key,expected", [
    ("anthropic", "", "", None),         # Claude can't listen
    ("anthropic", "sk", "", "openai"),   # falls back to an OpenAI key
    ("anthropic", "", "g", "gemini"),
    ("gemini", "sk", "g", "gemini"),     # the active provider first
])
def test_who_transcribes(monkeypatch, active, openai_key, gemini_key, expected):
    monkeypatch.setattr(settings, "ai_provider", active)
    monkeypatch.setattr(settings, "anthropic_api_key", "a")
    monkeypatch.setattr(settings, "openai_api_key", openai_key)
    monkeypatch.setattr(settings, "gemini_api_key", gemini_key)
    assert llm_provider.audio_provider() == expected


def test_openai_transcription(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "openai_api_key", "sk")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    sent = {}

    class FakeOpenAI:
        def __init__(self, **kw):
            self.audio = types.SimpleNamespace(transcriptions=types.SimpleNamespace(
                create=lambda **k: sent.update(k) or types.SimpleNamespace(text=" 200 g Mehl ")))
    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    text, _usage = llm_provider.transcribe_audio(b"x", "note.m4a")
    assert text == "200 g Mehl" and sent["model"] == settings.openai_transcribe_model
    assert sent["file"] == ("note.m4a", b"x", "audio/mp4")


def test_without_a_key_a_clear_message(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    with pytest.raises(RuntimeError, match="OpenAI or Gemini"):
        llm_provider.transcribe_audio(b"x", "note.m4a")
