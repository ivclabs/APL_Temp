"""Shared test setup: no real AI calls, a throwaway database for every test."""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Must be set before app/ai are imported anywhere.
os.environ["AI_BACKEND"] = "off"
os.environ["API_KEY"] = "test-key"
os.environ["DB_PATH"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeClaude:
    """Stands in for the real client: records requests, returns a fixed answer."""

    def __init__(self, text="Fake AI answer", stop_reason="end_turn"):
        self.text, self.stop_reason, self.requests = text, stop_reason, []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )


class RunNow:
    """Runs background jobs immediately, so tests can check the result right away."""

    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def server(monkeypatch, tmp_path):
    import ai
    import app as server

    db = str(tmp_path / "test.db")
    monkeypatch.setattr(server, "DB_PATH", db)
    monkeypatch.setattr(server, "ai_jobs", RunNow())
    server.init_db()
    return server


@pytest.fixture
def fake_ai(monkeypatch):
    """Turn AI on, but with the fake client."""
    import ai

    fake = FakeClaude()
    monkeypatch.setattr(ai, "BACKEND", "anthropic")
    monkeypatch.setattr(ai, "MODEL", "claude-haiku-5-5")
    monkeypatch.setattr(ai, "_client", fake)
    return fake


class FakeSNS:
    """Stands in for AWS SNS: records emails instead of sending them."""

    def __init__(self):
        self.sent = []

    def publish(self, **kwargs):
        self.sent.append(kwargs)
        return {"MessageId": "fake"}


@pytest.fixture
def fake_sns(monkeypatch):
    import notify

    fake = FakeSNS()
    monkeypatch.setattr(notify, "TOPIC_ARN", "arn:aws:sns:eu-north-1:123456789012:test")
    monkeypatch.setattr(notify, "PUBLIC_URL", "http://example.test")
    monkeypatch.setattr(notify, "_sns", fake)
    return fake
