"""Append-only audit log — the auditability guarantee from the issue: "a
maintainer must be able to review a full week of bot activity in under
five minutes." Every agent action is one row here.

Uses SQLModel (not raw SQLAlchemy): the same `AuditEntry` class is both the
Pydantic schema and the ORM table, since `config.py` already standardizes
on Pydantic — one less conceptual layer.

Three columns are unused today on purpose (`parent_run_id`, `overridden_at`
/`overridden_by`, `memory_refs`) — forward-compatible seams for Dimensions
2/3/4 of the extra-features roadmap. Adding those features later means
populating existing nullable columns, not a migration.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlmodel import Field, Session, SQLModel, create_engine, select


class AuditEntry(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # What triggered this run.
    trigger_event: str  # e.g. "pull_request.opened", "issues.opened"
    external_id: str  # e.g. "gh-pr-123", links back to the source Event
    workflow_name: str  # e.g. "dependabot_auto_merge", "issue_triage"

    # What the agent decided and why.
    agent_decision: str  # short machine-readable outcome, e.g. "merged" / "held"
    decision_reason: str | None = None  # which rule fired (merge_policy.py) or classifier output
    agent_reasoning_summary: str | None = None  # ResultMessage.result — the agent's own text

    # What actually happened.
    action_taken: str  # the tool call(s) made, e.g. "approve_and_merge_pr"
    action_result: str  # "success" / "failed: <reason>" / "skipped: kill switch engaged"
    can_be_reverted: bool = True
    revert_command: str | None = None

    # Cost/observability (Dimension 7 — already free once ResultMessage is logged).
    total_cost_usd: float | None = None
    duration_ms: int | None = None

    # --- Forward-compatible, unused in the MVP ---
    parent_run_id: int | None = None  # subagent hierarchies (Dimension 2)
    overridden_at: datetime | None = None  # maintainer reverted this decision (Dimension 4)
    overridden_by: str | None = None
    memory_refs: str | None = None  # JSON list of prior AuditEntry ids drawn on (Dimension 3)


def get_engine(db_path: str = "audit.sqlite3"):
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    return engine


class AuditWriter:
    """Every agent gets one of these and calls `.write(...)` exactly once
    per run, after draining the SDK's `query()` stream."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def write(self, **kwargs) -> AuditEntry:
        entry = AuditEntry(**kwargs)
        with Session(self.engine) as session:
            session.add(entry)
            session.commit()
            session.refresh(entry)
        return entry

    def recent(self, limit: int = 50) -> list[AuditEntry]:
        with Session(self.engine) as session:
            statement = select(AuditEntry).order_by(AuditEntry.timestamp.desc()).limit(limit)
            return list(session.exec(statement))

    def mark_overridden(self, entry_id: int, by: str) -> None:
        """Called by a future webhook listener on label-removed/comment-edited
        events (Dimension 4's raw signal). Not wired to any event source yet."""
        with Session(self.engine) as session:
            entry = session.get(AuditEntry, entry_id)
            if entry is None:
                return
            entry.overridden_at = datetime.now(timezone.utc)
            entry.overridden_by = by
            session.add(entry)
            session.commit()

    def stats(self, since_days: int = 7) -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        with Session(self.engine) as session:
            statement = select(AuditEntry).where(AuditEntry.timestamp >= cutoff)
            entries = list(session.exec(statement))

        by_workflow: dict[str, int] = {}
        for e in entries:
            by_workflow[e.workflow_name] = by_workflow.get(e.workflow_name, 0) + 1

        return {
            "since_days": since_days,
            "total_actions": len(entries),
            "by_workflow": by_workflow,
            "overridden_count": sum(1 for e in entries if e.overridden_at is not None),
            "total_cost_usd": round(sum(e.total_cost_usd or 0.0 for e in entries), 4),
        }


def entry_to_dict(entry: AuditEntry) -> dict:
    """JSON-serializable view for the dashboard's /api/audit endpoint."""
    data = entry.model_dump()
    data["timestamp"] = entry.timestamp.isoformat()
    if entry.overridden_at is not None:
        data["overridden_at"] = entry.overridden_at.isoformat()
    if entry.memory_refs:
        data["memory_refs"] = json.loads(entry.memory_refs)
    return data
