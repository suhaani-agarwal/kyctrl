"""Cron-triggered refresh of the Q&A assistant's doc graph — the
`source="cron", type="doc-index-refresh"` counterpart to `pattern_agent.py`.
No LLM judgment here at all: this just re-runs the two-step pipeline
(`scripts/crawl_docs.py`'s crawl functions, then
`scripts/build_doc_index.py`'s `build_index()`) and logs the result. Because
LightRAG dedups/updates by content hash, re-running this against a mostly-
unchanged crawl is cheap — see `tools/doc_retriever.py`'s docstring.

Registered on `"doc-index-refresh"`, dispatched by
`POST /internal/cron/doc-index-refresh` (see main.py) — a scheduled GitHub
Actions workflow calls that endpoint on a `schedule:` trigger.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from src.audit import AuditEntry
from src.config import kill_switch_engaged
from src.events import Event, register_handler
from src.runtime import get_audit_writer, get_config, get_repo_variable

WORKFLOW = "qa_assistant"  # shares the qa_assistant workflow toggle — refreshing its index is part of the same feature, not a separately-gated one.


async def handle_doc_index_refresh(external_id: str) -> AuditEntry:
    config = get_config()
    audit = get_audit_writer()

    if kill_switch_engaged(config, get_repo_variable):
        return audit.write(
            trigger_event="cron",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason="kill switch engaged",
            action_taken="none",
            action_result="skipped: kill switch engaged",
        )

    if not config.workflow_enabled(WORKFLOW):
        return audit.write(
            trigger_event="cron",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason="workflow disabled in config",
            action_taken="none",
            action_result="skipped: workflow disabled",
        )

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from scripts.build_doc_index import build_index  # noqa: E402
    from scripts.crawl_docs import crawl_docs_site, crawl_resolved_question_issues  # noqa: E402

    try:
        n_docs = crawl_docs_site()
        n_issues = crawl_resolved_question_issues()
        n_indexed = await build_index()
        return audit.write(
            trigger_event="cron",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="refreshed",
            decision_reason=f"crawled {n_docs} doc page(s), {n_issues} resolved question issue(s)",
            action_taken="crawl_docs,build_doc_index",
            action_result=f"success: {n_indexed} document(s) indexed",
            can_be_reverted=False,
        )
    except Exception as e:
        logger.error(f"doc-index-refresh failed: {e}")
        return audit.write(
            trigger_event="cron",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="failed",
            decision_reason=str(e),
            action_taken="crawl_docs,build_doc_index",
            action_result=f"failed: {e}",
            can_be_reverted=False,
        )


@register_handler("doc-index-refresh")
async def handle(event: Event) -> None:
    await handle_doc_index_refresh(event.external_id)
