"""Covers rate_limit_exceeded() in src/runtime.py — the enforcement side of
`.github/ai-maintainer.yaml`'s `rate_limits` block, which was previously
loaded into config and never actually checked anywhere (see
tests/test_dependabot_agent.py for the agent-level skip-path test)."""

from datetime import datetime, timedelta, timezone

from sqlmodel import Session

from src.audit import AuditWriter, get_engine
from src.config import AiMaintainerConfig
from src.runtime import rate_limit_exceeded


def make_writer(tmp_path):
    return AuditWriter(get_engine(str(tmp_path / "audit.sqlite3")))


def seed(writer, workflow, *, count, action_taken="approve_and_merge_pr", minutes_ago=0):
    for _ in range(count):
        entry = writer.write(
            trigger_event="pull_request",
            external_id="gh-pr-x",
            workflow_name=workflow,
            agent_decision="merge",
            action_taken=action_taken,
            action_result="success",
        )
        if minutes_ago:
            with Session(writer.engine) as session:
                db_entry = session.get(type(entry), entry.id)
                db_entry.timestamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
                session.add(db_entry)
                session.commit()


def test_rate_limit_not_exceeded_when_no_limit_configured(monkeypatch, tmp_path):
    writer = make_writer(tmp_path)
    monkeypatch.setattr("src.runtime.get_audit_writer", lambda: writer)
    seed(writer, "dependabot_auto_merge", count=100)
    config = AiMaintainerConfig(rate_limits={})
    assert rate_limit_exceeded(config, "dependabot_auto_merge") is False


def test_rate_limit_exceeded_true_when_over_threshold(monkeypatch, tmp_path):
    writer = make_writer(tmp_path)
    monkeypatch.setattr("src.runtime.get_audit_writer", lambda: writer)
    seed(writer, "dependabot_auto_merge", count=10)
    config = AiMaintainerConfig(rate_limits={"dependabot_auto_merge": 10})
    assert rate_limit_exceeded(config, "dependabot_auto_merge") is True


def test_rate_limit_not_exceeded_below_threshold(monkeypatch, tmp_path):
    writer = make_writer(tmp_path)
    monkeypatch.setattr("src.runtime.get_audit_writer", lambda: writer)
    seed(writer, "dependabot_auto_merge", count=9)
    config = AiMaintainerConfig(rate_limits={"dependabot_auto_merge": 10})
    assert rate_limit_exceeded(config, "dependabot_auto_merge") is False


def test_rate_limit_excludes_skipped_actions(monkeypatch, tmp_path):
    """A storm of instant kill-switch/disabled-workflow skips (action_taken
    == "none") isn't the "runaway behavior" rate_limits guards against —
    only rows recording a real action count toward the limit."""
    writer = make_writer(tmp_path)
    monkeypatch.setattr("src.runtime.get_audit_writer", lambda: writer)
    seed(writer, "dependabot_auto_merge", count=10, action_taken="none")
    config = AiMaintainerConfig(rate_limits={"dependabot_auto_merge": 10})
    assert rate_limit_exceeded(config, "dependabot_auto_merge") is False


def test_rate_limit_ignores_entries_older_than_an_hour(monkeypatch, tmp_path):
    writer = make_writer(tmp_path)
    monkeypatch.setattr("src.runtime.get_audit_writer", lambda: writer)
    seed(writer, "dependabot_auto_merge", count=10, minutes_ago=90)
    config = AiMaintainerConfig(rate_limits={"dependabot_auto_merge": 10})
    assert rate_limit_exceeded(config, "dependabot_auto_merge") is False


def test_rate_limit_is_scoped_per_workflow(monkeypatch, tmp_path):
    writer = make_writer(tmp_path)
    monkeypatch.setattr("src.runtime.get_audit_writer", lambda: writer)
    seed(writer, "issue_triage", count=50)  # unrelated workflow, well over any reasonable limit
    config = AiMaintainerConfig(rate_limits={"dependabot_auto_merge": 10})
    assert rate_limit_exceeded(config, "dependabot_auto_merge") is False
