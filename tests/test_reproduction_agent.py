from unittest.mock import MagicMock

import pytest

from src.agents.reproduction import handle_workflow_run, trigger_reproduction
from src.audit import AuditWriter, get_engine
from src.config import AiMaintainerConfig
from src.events import Event


def patch_runtime(monkeypatch, tmp_path, *, config: AiMaintainerConfig, issue=None):
    writer = AuditWriter(get_engine(str(tmp_path / "audit.sqlite3")))
    monkeypatch.setattr("src.agents.reproduction.get_config", lambda: config)
    monkeypatch.setattr("src.agents.reproduction.get_audit_writer", lambda: writer)
    monkeypatch.setattr("src.agents.reproduction.get_target_repo", lambda: "suhaani-agarwal/kyctrl-demo-target")
    monkeypatch.setattr("src.agents.reproduction.get_repo_variable", lambda name: None)

    gh = MagicMock()
    if issue is not None:
        gh.get_repo.return_value.get_issue.return_value = issue
    monkeypatch.setattr("src.agents.reproduction.get_client", lambda: gh)
    return writer


@pytest.mark.asyncio
async def test_skips_when_kill_switch_engaged(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=False, workflows={"reproduction_agent": True})
    patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await trigger_reproduction(1, "gh-issue-1-repro")

    assert entry.agent_decision == "skipped"
    assert "kill switch" in entry.decision_reason


@pytest.mark.asyncio
async def test_skips_when_workflow_disabled(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"reproduction_agent": False})
    patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await trigger_reproduction(1, "gh-issue-1-repro")

    assert entry.agent_decision == "skipped"
    assert "disabled" in entry.decision_reason


@pytest.mark.asyncio
async def test_skips_when_no_policy_manifest_in_body(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"reproduction_agent": True})
    issue = MagicMock()
    issue.body = "no manifest here at all"
    patch_runtime(monkeypatch, tmp_path, config=config, issue=issue)

    entry = await trigger_reproduction(1, "gh-issue-1-repro")

    assert entry.agent_decision == "skipped"
    assert "nothing to reproduce" in entry.action_result


@pytest.mark.asyncio
async def test_dispatches_when_reproducible(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"reproduction_agent": True})
    issue = MagicMock()
    issue.body = "```yaml\napiVersion: kyverno.io/v1\nkind: ClusterPolicy\nmetadata:\n  name: x\nspec:\n  rules: []\n```"
    patch_runtime(monkeypatch, tmp_path, config=config, issue=issue)
    monkeypatch.setattr("src.agents.reproduction.dispatch_reproduction_workflow", lambda *a, **kw: True)

    entry = await trigger_reproduction(1, "gh-issue-1-repro", parent_run_id=42)

    assert entry.agent_decision == "dispatched"
    assert entry.action_result == "dispatched, pending completion"
    assert entry.parent_run_id == 42


@pytest.mark.asyncio
async def test_workflow_run_completed_writes_audit_entry(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"reproduction_agent": True})
    writer = patch_runtime(monkeypatch, tmp_path, config=config)

    event = Event(
        source="github",
        type="workflow_run",
        action="completed",
        external_id="gh-workflow-run-999",
        payload={"workflow_run": {"id": 999, "path": ".github/workflows/reproduce-issue.yaml", "conclusion": "success"}},
    )
    await handle_workflow_run(event)

    entries = writer.recent()
    assert entries[0].agent_decision == "completed"
    assert entries[0].action_result == "success"


@pytest.mark.asyncio
async def test_workflow_run_ignores_other_workflows(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"reproduction_agent": True})
    writer = patch_runtime(monkeypatch, tmp_path, config=config)

    event = Event(
        source="github",
        type="workflow_run",
        action="completed",
        external_id="gh-workflow-run-1",
        payload={"workflow_run": {"id": 1, "path": ".github/workflows/some-other-workflow.yaml", "conclusion": "success"}},
    )
    await handle_workflow_run(event)

    assert writer.recent() == []
