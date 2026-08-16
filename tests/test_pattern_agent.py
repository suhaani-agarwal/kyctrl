from unittest.mock import MagicMock

import pytest

from src.agents.pattern_agent import handle_pattern_run
from src.audit import AuditWriter, get_engine
from src.config import AiMaintainerConfig


def patch_runtime(monkeypatch, tmp_path, *, config: AiMaintainerConfig, search_results=None):
    writer = AuditWriter(get_engine(str(tmp_path / "audit.sqlite3")))
    monkeypatch.setattr("src.agents.pattern_agent.get_config", lambda: config)
    monkeypatch.setattr("src.agents.pattern_agent.get_audit_writer", lambda: writer)
    monkeypatch.setattr("src.agents.pattern_agent.get_target_repo", lambda: "suhaani-agarwal/kyctrl-demo-target")
    monkeypatch.setattr("src.agents.pattern_agent.get_repo_variable", lambda name: None)

    gh = MagicMock()
    gh.search_issues.return_value = search_results or []
    monkeypatch.setattr("src.agents.pattern_agent.get_client", lambda: gh)
    return writer


@pytest.mark.asyncio
async def test_skips_when_kill_switch_engaged(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=False, workflows={"pattern_agent": True})
    patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await handle_pattern_run("cron-pattern-agent-1")

    assert entry.agent_decision == "skipped"
    assert "kill switch" in entry.decision_reason


@pytest.mark.asyncio
async def test_skips_when_workflow_disabled(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"pattern_agent": False})
    patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await handle_pattern_run("cron-pattern-agent-1")

    assert entry.agent_decision == "skipped"
    assert "disabled" in entry.decision_reason


@pytest.mark.asyncio
async def test_no_clusters_found_does_not_invoke_the_agent(monkeypatch, tmp_path):
    """No GitHub search results at all -> no cluster can reach min size ->
    the run should short-circuit before ever touching the Claude Agent SDK."""
    config = AiMaintainerConfig(enabled=True, workflows={"pattern_agent": True})
    patch_runtime(monkeypatch, tmp_path, config=config, search_results=[])

    entry = await handle_pattern_run("cron-pattern-agent-1")

    assert entry.agent_decision == "no_clusters"
    assert entry.action_taken == "none"
