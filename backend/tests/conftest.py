import os
import sys

import pytest

# Make "app" importable when pytest runs from backend/ (or the repo root).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from app import tandoor_client, tool_jobs  # noqa: E402
from app.config import settings  # noqa: E402
from fake_tandoor import FakeTandoor  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own data directory, no leftover tool runs and a
    configured (but fake) Tandoor/AI setup."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "output_language", "Deutsch")
    monkeypatch.setattr(settings, "tandoor_url", "http://tandoor")
    monkeypatch.setattr(settings, "tandoor_token", "test")
    with tool_jobs._lock:
        tool_jobs._tool_jobs.clear()
        tool_jobs._last_written.clear()
    yield tmp_path


@pytest.fixture
def tandoor(monkeypatch):
    """A fresh in-memory Tandoor that every get_client() call talks to."""
    fake = FakeTandoor()
    monkeypatch.setattr(tandoor_client, "get_client", fake.client)
    return fake
