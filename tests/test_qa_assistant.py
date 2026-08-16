import pytest

from src.agents.qa_assistant import (
    _post_answer,
    answer_question,
    decide_post_or_escalate,
    validate_citations,
)
from src.audit import AuditWriter, get_engine
from src.config import AiMaintainerConfig


def patch_runtime(monkeypatch, tmp_path, *, config: AiMaintainerConfig):
    writer = AuditWriter(get_engine(str(tmp_path / "audit.sqlite3")))
    monkeypatch.setattr("src.agents.qa_assistant.get_config", lambda: config)
    monkeypatch.setattr("src.agents.qa_assistant.get_audit_writer", lambda: writer)
    monkeypatch.setattr("src.agents.qa_assistant.get_repo_variable", lambda name: None)
    return writer


# --- validate_citations: the "never answer without a real citation" guardrail ---


def test_validate_citations_rejects_empty_citations():
    error = validate_citations([], set(), "high")
    assert error is not None
    assert "non-empty" in error


def test_validate_citations_rejects_a_url_search_docs_never_returned():
    error = validate_citations(["https://kyverno.io/docs/made-up"], {"https://kyverno.io/docs/real"}, "high")
    assert error is not None
    assert "never returned by search_docs" in error


def test_validate_citations_rejects_invalid_confidence():
    error = validate_citations(["https://kyverno.io/docs/real"], {"https://kyverno.io/docs/real"}, "very-sure")
    assert error is not None
    assert "confidence" in error


def test_validate_citations_accepts_real_citation_and_valid_confidence():
    error = validate_citations(["https://kyverno.io/docs/real"], {"https://kyverno.io/docs/real"}, "high")
    assert error is None


# --- decide_post_or_escalate: the confidence-threshold policy gate ---


def test_decide_posts_when_confidence_meets_threshold():
    proposal = {"answer": "yes, use verifyImages", "citations": ["u1"], "confidence": "high"}
    decision, reason = decide_post_or_escalate(proposal, "high")
    assert decision == "answer"
    assert "high" in reason


def test_decide_escalates_when_confidence_below_threshold():
    proposal = {"answer": "maybe?", "citations": ["u1"], "confidence": "medium"}
    decision, reason = decide_post_or_escalate(proposal, "high")
    assert decision == "escalate"
    assert "below threshold" in reason


def test_decide_escalates_when_no_answer_was_proposed():
    decision, reason = decide_post_or_escalate({}, "high")
    assert decision == "escalate"
    assert "did not propose" in reason


def test_decide_respects_a_lowered_threshold():
    proposal = {"answer": "probably", "citations": ["u1"], "confidence": "medium"}
    decision, _ = decide_post_or_escalate(proposal, "medium")
    assert decision == "answer"


# --- _post_answer: routes to the right adapter by source ---


def test_post_answer_routes_slack_sources_to_slack(monkeypatch):
    calls = []
    monkeypatch.setattr("src.agents.qa_assistant.post_message", lambda channel, text, thread_ts=None: calls.append((channel, thread_ts)) or True)
    assert _post_answer("slack", "kyverno:12345.6789", "the answer") is True
    assert calls == [("kyverno", "12345.6789")]


def test_post_answer_routes_discussion_sources_to_graphql(monkeypatch):
    calls = []
    monkeypatch.setattr("src.agents.qa_assistant.add_discussion_comment", lambda node_id, body, token=None: calls.append(node_id) or True)
    monkeypatch.setattr("src.agents.qa_assistant.get_github_token", lambda: "tok")
    assert _post_answer("github_discussion", "D_abc123", "the answer") is True
    assert calls == ["D_abc123"]


def test_post_answer_unknown_source_returns_false():
    assert _post_answer("carrier-pigeon", "n/a", "the answer") is False


# --- answer_question: skip-paths only, same philosophy as the other agents ---


@pytest.mark.asyncio
async def test_skips_when_kill_switch_engaged(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=False, workflows={"qa_assistant": True})
    patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await answer_question("Does Kyverno support X?", "slack", "kyverno:123", "slack-1")

    assert entry.agent_decision == "skipped"
    assert "kill switch" in entry.decision_reason


@pytest.mark.asyncio
async def test_skips_when_workflow_disabled(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"qa_assistant": False})
    patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await answer_question("Does Kyverno support X?", "slack", "kyverno:123", "slack-1")

    assert entry.agent_decision == "skipped"
    assert "disabled" in entry.decision_reason
