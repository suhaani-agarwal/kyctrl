from unittest.mock import MagicMock

import pytest

from src.agents.security_agent import handle_security_issue
from src.audit import AuditWriter, get_engine
from src.config import AiMaintainerConfig


def patch_runtime(monkeypatch, tmp_path, *, config: AiMaintainerConfig, issue=None):
    writer = AuditWriter(get_engine(str(tmp_path / "audit.sqlite3")))
    monkeypatch.setattr("src.agents.security_agent.get_config", lambda: config)
    monkeypatch.setattr("src.agents.security_agent.get_audit_writer", lambda: writer)
    monkeypatch.setattr("src.agents.security_agent.get_target_repo", lambda: "suhaani-agarwal/kyctrl-demo-target")
    monkeypatch.setattr("src.agents.security_agent.get_repo_variable", lambda name: None)

    gh = MagicMock()
    if issue is not None:
        gh.get_repo.return_value.get_issue.return_value = issue
    monkeypatch.setattr("src.agents.security_agent.get_client", lambda: gh)
    return writer


@pytest.mark.asyncio
async def test_skips_when_kill_switch_engaged(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=False, workflows={"security_agent": True})
    patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await handle_security_issue(1, "gh-issue-1")

    assert entry.agent_decision == "skipped"
    assert "kill switch" in entry.decision_reason


@pytest.mark.asyncio
async def test_skips_when_workflow_disabled(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"security_agent": False})
    patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await handle_security_issue(1, "gh-issue-1")

    assert entry.agent_decision == "skipped"
    assert "disabled" in entry.decision_reason


@pytest.mark.asyncio
async def test_skips_issue_without_trigger_label(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"security_agent": True})
    issue = MagicMock()
    label = MagicMock()
    label.name = "bug"
    issue.get_labels.return_value = [label]
    patch_runtime(monkeypatch, tmp_path, config=config, issue=issue)

    entry = await handle_security_issue(1, "gh-issue-1")

    assert entry.agent_decision == "skipped"
    assert "not a security-labeled issue" in entry.action_result
