"""Reproduction Agent — kyctrl_extra_features.md Dimension 2: "expensive and
only fires when Triage confirms a complete bug report ... runs real
clusters, has elevated capabilities."

Deliberately scoped down to its two deterministic halves for this pass —
the KinD cluster work itself lives entirely in
`.github/workflows/reproduce-issue.yaml`, which posts its own findings
directly to the issue via `gh issue comment` using the Action's own
`GITHUB_TOKEN`. kyctrl's Python side never parses reproduction output; it
only decides *whether* to dispatch, and records that the workflow
completed.

1. `trigger_reproduction()` — called by `issue_triage.py`'s deterministic
   handoff (a bug report with no missing fields, not security-labeled).
   Extracts manifests, dispatches the workflow, writes an audit entry.
2. `handle_workflow_run` — a second, independent audit entry when the
   dispatched workflow finishes, from the `workflow_run` webhook's own
   success/failure signal. Precise dispatch->run correlation (linking this
   entry's `parent_run_id` back to the exact `trigger_reproduction` call
   that caused it) isn't implemented yet — `create_dispatch` doesn't return
   a run id, so doing this properly needs a correlation token round-tripped
   through the workflow's inputs/outputs, not a guess. Flagged here rather
   than silently faked.
"""

from __future__ import annotations

import yaml
from loguru import logger

from src.agents.issue_reproduction_fields import extract_manifests_from_issue_body
from src.audit import AuditEntry
from src.config import kill_switch_engaged
from src.events import Event, register_handler
from src.runtime import get_audit_writer, get_client, get_config, get_repo_variable, get_target_repo
from src.tools.github_tools import dispatch_reproduction_workflow

WORKFLOW = "reproduction_agent"


async def trigger_reproduction(issue_number: int, external_id: str, parent_run_id: int | None = None) -> AuditEntry:
    config = get_config()
    audit = get_audit_writer()

    if kill_switch_engaged(config, get_repo_variable):
        return audit.write(
            trigger_event="issues",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason="kill switch engaged",
            action_taken="none",
            action_result="skipped: kill switch engaged",
            parent_run_id=parent_run_id,
        )

    if not config.workflow_enabled(WORKFLOW):
        return audit.write(
            trigger_event="issues",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason="workflow disabled in config",
            action_taken="none",
            action_result="skipped: workflow disabled",
            parent_run_id=parent_run_id,
        )

    repo_full_name = get_target_repo()
    gh = get_client()
    repo = gh.get_repo(repo_full_name)
    issue = repo.get_issue(issue_number)

    manifests = extract_manifests_from_issue_body(issue.body)
    if not manifests.is_reproducible:
        return audit.write(
            trigger_event="issues",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason="no policy manifest found in issue body",
            action_taken="none",
            action_result="skipped: nothing to reproduce",
            parent_run_id=parent_run_id,
        )

    inputs = {
        "issue_number": str(issue_number),
        "policy_yaml": yaml.dump_all(manifests.policy_manifests),
        "resource_yaml": yaml.dump_all(manifests.resource_manifests) if manifests.resource_manifests else "",
    }
    dispatched = dispatch_reproduction_workflow(gh, repo_full_name, config.reproduction_agent.workflow_file, inputs)

    return audit.write(
        trigger_event="issues",
        external_id=external_id,
        workflow_name=WORKFLOW,
        agent_decision="dispatched" if dispatched else "dispatch_failed",
        decision_reason=(
            f"{len(manifests.policy_manifests)} policy manifest(s), "
            f"{len(manifests.resource_manifests)} resource manifest(s) extracted"
        ),
        action_taken="dispatch_reproduction_workflow",
        action_result="dispatched, pending completion" if dispatched else "failed: workflow dispatch call failed",
        can_be_reverted=False,
        revert_command="N/A — read-only reproduction run, nothing to revert",
        parent_run_id=parent_run_id,
    )


@register_handler("workflow_run")
async def handle_workflow_run(event: Event) -> None:
    config = get_config()
    if not config.workflow_enabled(WORKFLOW):
        return
    wr = event.payload.get("workflow_run", {})
    if event.action != "completed":
        return
    if config.reproduction_agent.workflow_file not in (wr.get("path") or ""):
        return

    audit = get_audit_writer()
    conclusion = wr.get("conclusion", "unknown")
    logger.info(f"Reproduction workflow run {wr.get('id')} completed: {conclusion}")
    audit.write(
        trigger_event="workflow_run",
        external_id=event.external_id,
        workflow_name=WORKFLOW,
        agent_decision="completed",
        decision_reason=f"conclusion={conclusion}",
        action_taken="none (workflow posted its own findings via gh issue comment)",
        action_result="success" if conclusion == "success" else f"failed: {conclusion}",
        can_be_reverted=False,
    )
