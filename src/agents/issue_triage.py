"""Issue triage agent: classification (mostly already done by Kyverno's own
issue templates — see skills/kyverno/issue-triage.md), missing-info
detection for bug reports (deterministic, via issue_fields.py), and the
label-FSM transition (deterministic, via issue_fsm.py). The SDK is used
for exactly the part that's genuinely a judgment call: writing the comment,
and recognizing when an issue is actually a misfiled question/docs/policy
request despite its template label — Kyverno redirects those via
`.github/ISSUE_TEMPLATE/config.yml`'s contact_links, and this agent applies
the same policy to the rare ones that slip through anyway.
"""

from __future__ import annotations

import json

from claude_agent_sdk import (
    ClaudeAgentOptions,
    SandboxNetworkConfig,
    SandboxSettings,
    create_sdk_mcp_server,
    query,
    tool,
)
from github import GithubException
from loguru import logger

from src.agents._shared import memory_search, memory_write
from src.agents.issue_fields import missing_bug_fields, uses_webhook_template
from src.agents.issue_fsm import STATES, state_label, validate_transition
from src.agents.reproduction import trigger_reproduction
from src.audit import AuditEntry
from src.config import kill_switch_engaged
from src.events import Event, register_handler
from src.memory import build_memory_tool_server
from src.runtime import (
    can_use_tool,
    get_audit_writer,
    get_client,
    get_config,
    get_memory_client,
    get_repo_variable,
    get_target_repo,
    rate_limit_exceeded,
    single_turn_prompt,
)
from src.skills import load_skill
from src.terminal import stream_agent_run
from src.tools.github_tools import build_issue_tool_server

WORKFLOW = "issue_triage"


def _build_state_tool_server(issue, current_labels: set[str]):
    """The FSM decides which transitions are valid; this tool is the only
    way the agent can move the state, and it always checks
    `validate_transition` first — an invalid request comes back as a
    rejected tool result, not a silently-wrong label."""

    @tool(
        "transition_issue_state",
        f"Move this issue to a new triage state. Valid states: {', '.join(STATES)}.",
        {"target_state": str},
    )
    async def transition_issue_state(args: dict) -> dict:
        result = validate_transition(current_labels, args["target_state"])
        if not result.ok:
            return {"content": [{"type": "text", "text": f"REJECTED: {result.reason}"}], "is_error": True}
        try:
            if result.from_state:
                issue.remove_from_labels(state_label(result.from_state))
            issue.add_to_labels(state_label(result.to_state))
        except GithubException as e:
            logger.warning(f"GitHub tool call failed (transition_issue_state): {e}")
            return {
                "content": [{"type": "text", "text": f"transition_issue_state failed: {e}"}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": f"transitioned {result.from_state!r} -> {result.to_state!r}"}]}

    return create_sdk_mcp_server(name="issue-state-tools", tools=[transition_issue_state])


async def handle_issue_event(issue_number: int, external_id: str) -> AuditEntry:
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

    if rate_limit_exceeded(config, WORKFLOW):
        limit = config.rate_limits.get(WORKFLOW)
        return audit.write(
            trigger_event="issues",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason=f"rate limit exceeded ({limit}/hour)",
            action_taken="none",
            action_result="skipped: rate limit exceeded",
        )

    repo_full_name = get_target_repo()
    gh = get_client()
    repo = gh.get_repo(repo_full_name)
    issue = repo.get_issue(issue_number)
    labels = {label.name for label in issue.get_labels()}

    if labels & set(config.issue_triage.exclusion_labels):
        return audit.write(
            trigger_event="issues",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason=f"issue carries an exclusion label ({labels & set(config.issue_triage.exclusion_labels)})",
            action_taken="none",
            action_result="skipped: excluded by label",
        )

    policy = config.issue_triage
    is_bug = policy.labels.get("bug") in labels
    is_feature = policy.labels.get("feature") in labels

    required_fields = list(policy.required_bug_fields)
    if uses_webhook_template(issue.body):
        required_fields += policy.webhook_only_fields
    missing = missing_bug_fields(issue.body, required_fields) if is_bug else []

    classification = "bug" if is_bug else "feature" if is_feature else "unclassified"
    logger.info(f"Issue #{issue_number} classified as {classification}, missing fields: {missing}")

    state_tools = _build_state_tool_server(issue, labels)
    issue_tools = build_issue_tool_server(gh, repo_full_name, issue_number)
    skill = load_skill("issue-triage")

    # Prefetch relevant memory — see the matching comment in agents/dependabot.py.
    memory = get_memory_client()
    memory_facts = await memory_search(f"issue: {issue.title}")
    mcp_servers = {"github": issue_tools, "state": state_tools}
    if memory is not None:
        mcp_servers["memory"] = build_memory_tool_server(memory, default_limit=config.memory.search_top_k)

    options = ClaudeAgentOptions(
        system_prompt=skill,
        mcp_servers=mcp_servers,
        # See the matching comment in agents/dependabot.py.
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
        f"Issue #{issue_number}: {issue.title!r}\n"
        f"Existing labels: {sorted(labels)}\n"
        f"Deterministic classification: {classification}\n"
        f"Deterministic missing-field check (bug reports only): {missing or 'none'}\n"
        f"Relevant memory (past runs on similar issues): {memory_facts or 'none found'}\n\n"
        f"Body:\n{issue.body or '(empty)'}\n\n"
        f"First, read the body. If this is clearly NOT actually a bug/feature report — it's a "
        f"usage question or documentation feedback that slipped past the template picker — post "
        f"a comment(via comment_on_issue) pointing to the right place per the skill doc's "
        f"redirect guidance, then call transition_issue_state with target_state='redirected'. "
        f"Otherwise: if the missing-field list above is non-empty, post a comment naming exactly "
        f"which field(s) are missing and why (be specific, not a generic checklist), then call "
        f"transition_issue_state with target_state='needs-repro-info'. If nothing is missing, "
        f"post a short comment confirming the report looks complete and call "
        f"transition_issue_state with target_state='ready-for-human'."
    )

    result = await stream_agent_run(
        query(prompt=single_turn_prompt(prompt), options=options),
        title=f"Issue Triage Agent — #{issue_number} ({classification})",
    )

    action_result = "success"
    if result is None:
        action_result = "failed: no ResultMessage from agent run"
    elif result.is_error:
        action_result = f"failed: {result.subtype}"

    # Body excerpt included (not just the title) so anything a reporter
    # actually names — a suspected package, a component, a version — is
    # real, searchable content for a later memory_search() to surface, not
    # just whatever's in the (often generic) issue title.
    body_excerpt = (issue.body or "").strip().replace("\n", " ")[:400]
    memory_refs = await memory_write(
        name=f"{repo_full_name}:issue-{issue_number}",
        episode_body=(
            f"Issue #{issue_number} ({issue.title!r}): classified as {classification}, "
            f"missing_fields={missing or 'none'}. Agent action result: {action_result}. "
            f"Body excerpt: {body_excerpt!r}"
        ),
        source_description=WORKFLOW,
    )

    entry = audit.write(
        trigger_event="issues",
        external_id=external_id,
        workflow_name=WORKFLOW,
        agent_decision=classification,
        decision_reason=f"missing_fields={missing}" if missing else "complete or non-bug",
        agent_reasoning_summary=result.result if result else None,
        action_taken="comment_on_issue,transition_issue_state",
        action_result=action_result,
        can_be_reverted=True,
        revert_command="remove the ai/* label and delete the bot's comment",
        total_cost_usd=result.total_cost_usd if result else None,
        duration_ms=result.duration_ms if result else None,
        memory_refs=json.dumps(memory_refs) if memory_refs else None,
    )

    # Deterministic handoff to the Reproduction Agent: only a complete bug
    # report, never security-labeled (a second explicit guard — exclusion_labels
    # already keeps security issues out of this function entirely).
    if classification == "bug" and not missing and "security" not in labels:
        await trigger_reproduction(issue_number, f"{external_id}-repro", parent_run_id=entry.id)

    return entry


@register_handler("issues")
async def handle(event: Event) -> None:
    if event.action not in ("opened", "edited", "reopened"):
        return
    issue_number = event.payload.get("number") or event.payload.get("issue", {}).get("number")
    if issue_number is None:
        logger.warning("issues event missing issue number, skipping")
        return
    await handle_issue_event(issue_number, event.external_id)
