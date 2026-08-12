"""Covers the fast skip paths (kill switch, disabled workflow, non-bot
author) which don't touch the Claude Agent SDK or the network. The full
merge/hold path is exercised by test_merge_policy.py (deterministic logic)
plus manual end-to-end verification against the demo repo — mocking the
SDK's streaming `query()` end-to-end adds a lot of test-double complexity
for little extra confidence versus those two."""

from unittest.mock import MagicMock

import pytest

from src.agents.dependabot import handle_dependabot_pr
from src.audit import AuditWriter, get_engine
from src.config import AiMaintainerConfig


def patch_runtime(monkeypatch, tmp_path, *, config: AiMaintainerConfig, pr=None):
    writer = AuditWriter(get_engine(str(tmp_path / "audit.sqlite3")))
    monkeypatch.setattr("src.agents.dependabot.get_config", lambda: config)
    monkeypatch.setattr("src.agents.dependabot.get_audit_writer", lambda: writer)
    monkeypatch.setattr("src.agents.dependabot.get_target_repo", lambda: "suhaani-agarwal/kyctrl-demo-target")
    monkeypatch.setattr("src.agents.dependabot.get_repo_variable", lambda name: None)

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
async def test_skips_non_bot_author(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"dependabot_auto_merge": True})
    pr = MagicMock()
    pr.user.login = "some-human-contributor"
    patch_runtime(monkeypatch, tmp_path, config=config, pr=pr)

    entry = await handle_dependabot_pr(1, "gh-pr-1")

    assert entry.agent_decision == "skipped"
    assert "not in dependabot.bot_usernames" in entry.decision_reason
