import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from src.main import app, verify_signature


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("AUDIT_DB_PATH", str(tmp_path / "audit.sqlite3"))
    monkeypatch.setenv("AI_MAINTAINER_CONFIG_PATH", str(tmp_path / "missing.yaml"))
    # Force AuditWriter's lru_cache to rebuild against the temp DB path.
    from src.runtime import get_audit_writer

    get_audit_writer.cache_clear()
    yield
    get_audit_writer.cache_clear()


def sign(body: bytes, secret: str = "test-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_correct_hmac():
    body = b'{"a": 1}'
    assert verify_signature("test-secret", body, sign(body)) is True


def test_verify_signature_rejects_wrong_secret():
    body = b'{"a": 1}'
    assert verify_signature("test-secret", body, sign(body, secret="wrong")) is False


def test_verify_signature_rejects_missing_header():
    assert verify_signature("test-secret", b"{}", None) is False


def test_health_endpoint():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_webhook_rejects_bad_signature():
    client = TestClient(app)
    resp = client.post(
        "/webhook",
        content=json.dumps({"action": "opened"}),
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert resp.status_code == 401


def test_webhook_accepts_valid_signature_and_dispatches(monkeypatch):
    called = {}

    async def fake_handler(event):
        called["event"] = event

    monkeypatch.setitem(__import__("src.events", fromlist=["EVENT_HANDLERS"]).EVENT_HANDLERS, "pull_request", fake_handler)

    client = TestClient(app)
    body = json.dumps({"action": "opened", "pull_request": {"number": 42}}).encode()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
    assert called["event"].external_id == "gh-pr-42"
    assert called["event"].action == "opened"


def test_webhook_ignores_unknown_event_type():
    client = TestClient(app)
    body = json.dumps({"action": "starred"}).encode()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "star", "X-Hub-Signature-256": sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_api_audit_and_stats_empty_by_default():
    client = TestClient(app)
    assert client.get("/api/audit").json() == []
    stats = client.get("/api/stats").json()
    assert stats["total_actions"] == 0


def test_dashboard_serves_html():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
