"""Covers the fast skip paths (kill switch, disabled workflow, non-bot
author) which don't touch the Claude Agent SDK or the network. The full
merge/hold path is exercised by test_merge_policy.py (deterministic logic)
plus manual end-to-end verification against the demo repo — mocking the
SDK's streaming `query()` end-to-end adds a lot of test-double complexity
for little extra confidence versus those two."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agents.dependabot import handle_dependabot_pr, handle_status
from src.audit import AuditWriter, get_engine
from src.config import AiMaintainerConfig
from src.events import Event


def patch_runtime(monkeypatch, tmp_path, *, config: AiMaintainerConfig, pr=None):
    writer = AuditWriter(get_engine(str(tmp_path / "audit.sqlite3")))
    monkeypatch.setattr("src.agents.dependabot.get_config", lambda: config)
    monkeypatch.setattr("src.agents.dependabot.get_audit_writer", lambda: writer)
    monkeypatch.setattr("src.agents.dependabot.get_target_repo", lambda: "suhaani-agarwal/kyctrl-demo-target")
    monkeypatch.setattr("src.agents.dependabot.get_repo_variable", lambda name: None)
    # rate_limit_exceeded() lives in src.runtime and resolves get_audit_writer()
    # from that module's own namespace, not dependabot.py's imported copy —
    # same writer/tmp db either way, just patched at both call sites.
    monkeypatch.setattr("src.runtime.get_audit_writer", lambda: writer)

    gh = MagicMock()
    if pr is not None:
        gh.get_repo.return_value.get_pull.return_value = pr
    monkeypatch.setattr("src.agents.dependabot.get_client", lambda: gh)
    return writer


@pytest.mark.asyncio
async def test_skips_when_kill_switch_engaged(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=False, workflows={"dependabot_auto_merge": True})
    writer = patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await handle_dependabot_pr(1, "gh-pr-1")

    assert entry.agent_decision == "skipped"
    assert "kill switch" in entry.decision_reason
    assert writer.recent()[0].action_result == "skipped: kill switch engaged"


@pytest.mark.asyncio
async def test_skips_when_workflow_disabled(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"dependabot_auto_merge": False})
    patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await handle_dependabot_pr(1, "gh-pr-1")

    assert entry.agent_decision == "skipped"
    assert "disabled" in entry.decision_reason


@pytest.mark.asyncio
async def test_skips_when_rate_limit_exceeded(monkeypatch, tmp_path):
    config = AiMaintainerConfig(
        enabled=True, workflows={"dependabot_auto_merge": True}, rate_limits={"dependabot_auto_merge": 1}
    )
    writer = patch_runtime(monkeypatch, tmp_path, config=config)
    writer.write(
        trigger_event="pull_request",
        external_id="gh-pr-0",
        workflow_name="dependabot_auto_merge",
        agent_decision="merge",
        action_taken="approve_and_merge_pr",
        action_result="success",
    )

    entry = await handle_dependabot_pr(1, "gh-pr-1")

    assert entry.agent_decision == "skipped"
    assert "rate limit exceeded" in entry.decision_reason
    assert entry.action_result == "skipped: rate limit exceeded"


@pytest.mark.asyncio
async def test_skips_non_bot_author(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"dependabot_auto_merge": True})
    pr = MagicMock()
    pr.user.login = "some-human-contributor"
    patch_runtime(monkeypatch, tmp_path, config=config, pr=pr)

    entry = await handle_dependabot_pr(1, "gh-pr-1")

    assert entry.agent_decision == "skipped"
    assert "not in dependabot.bot_usernames" in entry.decision_reason


@pytest.mark.asyncio
async def test_status_event_reevaluates_open_prs_on_that_commit(monkeypatch):
    """Regression test for the race hit live against the demo repo: the
    `pull_request` webhook is often evaluated before CI finishes, so
    without reacting to `status` too, a PR that only goes green afterward
    would never be re-checked."""
    gh = MagicMock()
    repo = gh.get_repo.return_value
    open_pr = SimpleNamespace(number=1, state="open")
    closed_pr = SimpleNamespace(number=2, state="closed")
    repo.get_commit.return_value.get_pulls.return_value = [open_pr, closed_pr]
    monkeypatch.setattr("src.agents.dependabot.get_target_repo", lambda: "suhaani-agarwal/kyctrl-demo-target")
    monkeypatch.setattr("src.agents.dependabot.get_client", lambda: gh)

    calls = []

    async def fake_handle_dependabot_pr(pr_number, external_id):
        calls.append((pr_number, external_id))

    monkeypatch.setattr("src.agents.dependabot.handle_dependabot_pr", fake_handle_dependabot_pr)

    event = Event(
        source="github", type="status", action=None, external_id="gh-status-abc123def456", payload={"sha": "abc123def456"}
    )
    await handle_status(event)

    # Only the open PR is re-evaluated; the closed one is skipped.
    assert calls == [(1, "gh-status-abc123def456-pr1")]
