"""Security Agent — kyctrl_extra_features.md Dimension 2: "runs separately
from everything else, with no access to public comment posting. It reads
vulnerability reports, cross-references CVE databases, runs dependency
analysis, and produces a private report that goes only to maintainers —
never to the public issue thread."

Registers independently on `issues` (see events.py's fan-out), gated
internally on the `security` label — the same label
`skills/kyverno/issue-triage.md` documents as "never applied by
[issue_triage.py]; that label only comes from the automated scanning
workflow." `issue_triage.exclusion_labels` includes `security` in
`.github/ai-maintainer.yaml` precisely so the two agents never race on the
same issue: config keeps them apart, not a code dependency between the two
modules.

Dependency-graph cross-referencing / CVE-database lookups are not built in
this pass — this agent's job today is: read the report, produce a
structured private assessment, file it privately. Wiring a real CVE feed is
a natural next step once this path is proven.
"""

from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions, SandboxNetworkConfig, SandboxSettings, query
from loguru import logger

from src.audit import AuditEntry
from src.config import kill_switch_engaged
from src.events import Event, register_handler
from src.runtime import (
    can_use_tool,
    get_audit_writer,
    get_client,
    get_config,
    get_repo_variable,
    get_target_repo,
    single_turn_prompt,
)
from src.skills import load_skill
from src.terminal import stream_agent_run
from src.tools.security_tools import build_security_report_tool_server

WORKFLOW = "security_agent"


async def handle_security_issue(issue_number: int, external_id: str) -> AuditEntry:
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
        )

    repo_full_name = get_target_repo()
    gh = get_client()
    repo = gh.get_repo(repo_full_name)
    issue = repo.get_issue(issue_number)
    labels = {label.name for label in issue.get_labels()}

    trigger_labels = set(config.security_agent.trigger_labels)
    if not (labels & trigger_labels):
        return audit.write(
            trigger_event="issues",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason=f"no trigger label present (need one of {sorted(trigger_labels)})",
            action_taken="none",
            action_result="skipped: not a security-labeled issue",
        )

    tool_server = build_security_report_tool_server(issue_number, repo_full_name, config.security_agent.private_slack_channel)
    skill = load_skill("security-agent")

    options = ClaudeAgentOptions(
        system_prompt=skill,
        mcp_servers={"security": tool_server},
        tools=[],
        allowed_tools=[],
        can_use_tool=can_use_tool,
        max_turns=6,
        sandbox=SandboxSettings(
            enabled=True,
            network=SandboxNetworkConfig(allowedDomains=["api.github.com", "api.anthropic.com"]),
        ),
    )

    prompt = (
        f"Security-labeled issue #{issue_number}: {issue.title!r}\n\n"
        f"Body:\n{issue.body or '(empty)'}\n\n"
        f"Assess this report per the skill doc and call `file_private_report` with a severity "
        f"estimate, the affected component, and a summary a maintainer can act on quickly. "
        f"You have no way to comment publicly on this issue — do not attempt to."
    )

    result = await stream_agent_run(
        query(prompt=single_turn_prompt(prompt), options=options),
        title=f"Security Agent — #{issue_number} (private)",
    )

    action_result = "success"
    if result is None:
        action_result = "failed: no ResultMessage from agent run"
    elif result.is_error:
        action_result = f"failed: {result.subtype}"

    return audit.write(
        trigger_event="issues",
        external_id=external_id,
        workflow_name=WORKFLOW,
        agent_decision="private_report_filed",
        decision_reason=f"trigger labels present: {sorted(labels & trigger_labels)}",
        agent_reasoning_summary=result.result if result else None,
        action_taken="file_private_report",
        action_result=action_result,
        can_be_reverted=False,
        revert_command="N/A — private report only, nothing public to revert",
        total_cost_usd=result.total_cost_usd if result else None,
        duration_ms=result.duration_ms if result else None,
    )


@register_handler("issues")
async def handle(event: Event) -> None:
    if event.action not in ("opened", "labeled", "edited"):
        return
    issue_number = event.payload.get("number") or event.payload.get("issue", {}).get("number")
    if issue_number is None:
        logger.warning("issues event missing issue number, skipping (security_agent)")
        return
    await handle_security_issue(issue_number, f"{event.external_id}-security")
