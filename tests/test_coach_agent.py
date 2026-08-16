from unittest.mock import MagicMock

import pytest

from src.agents.coach import _touches_restricted_paths, handle_coach_pr
from src.audit import AuditWriter, get_engine
from src.config import AiMaintainerConfig, SafeBoundaries


def patch_runtime(monkeypatch, tmp_path, *, config: AiMaintainerConfig, pr=None):
    writer = AuditWriter(get_engine(str(tmp_path / "audit.sqlite3")))
    monkeypatch.setattr("src.agents.coach.get_config", lambda: config)
    monkeypatch.setattr("src.agents.coach.get_audit_writer", lambda: writer)
    monkeypatch.setattr("src.agents.coach.get_target_repo", lambda: "suhaani-agarwal/kyctrl-demo-target")
    monkeypatch.setattr("src.agents.coach.get_repo_variable", lambda name: None)

    gh = MagicMock()
    if pr is not None:
        gh.get_repo.return_value.get_pull.return_value = pr
    monkeypatch.setattr("src.agents.coach.get_client", lambda: gh)
    return writer


@pytest.mark.asyncio
async def test_skips_when_kill_switch_engaged(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=False, workflows={"coach_agent": True})
    patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await handle_coach_pr(1, "gh-pr-1")

    assert entry.agent_decision == "skipped"
    assert "kill switch" in entry.decision_reason


@pytest.mark.asyncio
async def test_skips_when_workflow_disabled(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"coach_agent": False})
    patch_runtime(monkeypatch, tmp_path, config=config)

    entry = await handle_coach_pr(1, "gh-pr-1")

    assert entry.agent_decision == "skipped"
    assert "disabled" in entry.decision_reason


@pytest.mark.asyncio
async def test_skips_bot_author(monkeypatch, tmp_path):
    config = AiMaintainerConfig(enabled=True, workflows={"coach_agent": True})
    pr = MagicMock()
    pr.user.login = "dependabot[bot]"
    patch_runtime(monkeypatch, tmp_path, config=config, pr=pr)

    entry = await handle_coach_pr(1, "gh-pr-1")

    assert entry.agent_decision == "skipped"
    assert "bot account" in entry.decision_reason


def test_touches_restricted_paths_detects_exact_and_prefix_matches():
    files = [{"filename": "api/kyverno/v1/policy_types.go"}, {"filename": "pkg/engine/validate.go"}]
    hits = _touches_restricted_paths(files, SafeBoundaries(restricted_paths=["api/kyverno/v1/", "pkg/cosign/"]).restricted_paths)
    assert hits == ["api/kyverno/v1/"]


def test_touches_restricted_paths_no_match():
    files = [{"filename": "pkg/engine/validate.go"}]
    hits = _touches_restricted_paths(files, ["api/kyverno/v1/"])
    assert hits == []
