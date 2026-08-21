"""Dependabot/Renovate auto-merge agent.

The merge/hold decision is made entirely by `merge_policy.evaluate` before
this module ever calls the Claude Agent SDK. Whether a hold needs
`needs-human-review` is also decided there and applied directly below, not
left to the model — that judgment call used to flip-flop across
otherwise-identical runs. This agent's only job is to explain the decision
in a PR comment and, only when the rule engine already cleared the PR,
call the merge tool — `build_pr_tool_server(..., allow_merge=)` doesn't
include `approve_and_merge_pr` at all otherwise.
"""

from __future__ import annotations

import json

from claude_agent_sdk import ClaudeAgentOptions, SandboxNetworkConfig, SandboxSettings, query
from github import GithubException
from loguru import logger

from src.agents._shared import memory_search, memory_write
from src.agents.merge_policy import evaluate
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
from src.tools.github_tools import build_pr_tool_server

WORKFLOW = "dependabot_auto_merge"


async def handle_dependabot_pr(pr_number: int, external_id: str) -> AuditEntry:
    config = get_config()
    audit = get_audit_writer()

    if kill_switch_engaged(config, get_repo_variable):
        return audit.write(
            trigger_event="pull_request",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason="kill switch engaged",
            action_taken="none",
            action_result="skipped: kill switch engaged",
            can_be_reverted=True,
        )

    if not config.workflow_enabled(WORKFLOW):
        return audit.write(
            trigger_event="pull_request",
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
            trigger_event="pull_request",
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
    pr = repo.get_pull(pr_number)

    if pr.user.login not in config.dependabot.bot_usernames:
        logger.info(f"PR #{pr_number} author {pr.user.login!r} is not a dependency bot — skipping")
        return audit.write(
            trigger_event="pull_request",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason=f"author {pr.user.login!r} not in dependabot.bot_usernames",
            action_taken="none",
            action_result="skipped: not a dependency PR",
        )

    decision = evaluate(pr, config.dependabot)
    logger.info(f"merge_policy decision for PR #{pr_number}: {decision.decision} ({decision.rule})")

    if decision.decision == "hold" and decision.needs_human_review:
        try:
            pr.add_to_labels(config.dependabot.needs_review_label)
        except GithubException as e:
            logger.warning(f"Could not apply {config.dependabot.needs_review_label!r} label to PR #{pr_number}: {e}")

    tool_server = build_pr_tool_server(gh, repo_full_name, pr_number, allow_merge=decision.decision == "merge")
    skill = load_skill("dependabot-policy")

    # Prefetch relevant memory (no-ops to [] if disabled/unreachable), and
    # only offer `search_memory` as a tool when it's actually available.
    memory = get_memory_client()
    memory_facts = await memory_search(f"dependency bump: {pr.title}")
    mcp_servers = {"github": tool_server}
    if memory is not None:
        mcp_servers["memory"] = build_memory_tool_server(memory, default_limit=config.memory.search_top_k)

    options = ClaudeAgentOptions(
        system_prompt=skill,
        mcp_servers=mcp_servers,
        # Empty tools/allowed_tools means every call falls through to
        # can_use_tool (see runtime.py) — the only place the kill switch
        # and tool allow-list are actually enforced.
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
        f"The deterministic policy engine already evaluated PR #{pr_number} "
        f"({pr.title!r}) and decided: **{decision.decision}** (rule: `{decision.rule}`). "
        f"Reason: {decision.reason}\n\n"
        f"Relevant memory (past runs on this or similar dependencies): "
        f"{memory_facts or 'none found'}\n\n"
        f"If the decision is 'merge': call `approve_and_merge_pr` with a `summary` "
        f"explaining exactly why this is safe, in one or two sentences referencing the "
        f"actual rule that fired. You may call `get_pr_diff` or `get_check_status` first "
        f"if you want to double-check anything, but do not second-guess the merge/hold "
        f"decision itself — only explain it.\n\n"
        f"If the decision is 'hold': call `comment_on_pr` with a clear, short comment "
        f"explaining the reason a contributor could understand in five seconds. "
        f"(The `needs-human-review` label, if this hold genuinely needs one, has already "
        f"been applied by the policy engine — don't mention labeling as something you're "
        f"about to do.)"
    )

    result = await stream_agent_run(
        query(prompt=single_turn_prompt(prompt), options=options),
        title=f"Dependabot Agent — PR #{pr_number} ({decision.decision})",
    )

    action_result = "success"
    if result is None:
        action_result = "failed: no ResultMessage from agent run"
    elif result.is_error:
        action_result = f"failed: {result.subtype}"

    memory_refs = await memory_write(
        name=f"{repo_full_name}:pr-{pr_number}",
        episode_body=(
            f"Dependabot/Renovate PR #{pr_number} ({pr.title!r}): policy decision "
            f"{decision.decision} (rule={decision.rule}, reason={decision.reason}). "
            f"Agent action result: {action_result}."
        ),
        source_description=WORKFLOW,
    )

    return audit.write(
        trigger_event="pull_request",
        external_id=external_id,
        workflow_name=WORKFLOW,
        agent_decision=decision.decision,
        decision_reason=f"{decision.rule}: {decision.reason}",
        agent_reasoning_summary=result.result if result else None,
        action_taken=(
            "approve_and_merge_pr"
            if decision.decision == "merge"
            else ("comment_on_pr,add_label" if decision.needs_human_review else "comment_on_pr")
        ),
        action_result=action_result,
        can_be_reverted=True,
        revert_command=(
            "git revert <squash-merge-commit-sha>" if decision.decision == "merge" else "N/A — comment/label only"
        ),
        total_cost_usd=result.total_cost_usd if result else None,
        duration_ms=result.duration_ms if result else None,
        memory_refs=json.dumps(memory_refs) if memory_refs else None,
    )


@register_handler("pull_request")
async def handle(event: Event) -> None:
    if event.action not in ("opened", "synchronize", "reopened"):
        return
    pr_number = event.payload.get("number") or event.payload.get("pull_request", {}).get("number")
    if pr_number is None:
        logger.warning("pull_request event missing PR number, skipping")
        return
    await handle_dependabot_pr(pr_number, event.external_id)


@register_handler("status")
async def handle_status(event: Event) -> None:
    """A `status` webhook fires when CI posts a commit status. Exists
    because of a real race: the `pull_request` event above usually gets
    evaluated before CI finishes, so a PR that only turns green afterward
    would otherwise hold forever. Re-runs the policy for every open PR
    whose head is this commit (almost always zero or one)."""
    sha = event.payload.get("sha")
    if not sha:
        return
    repo_full_name = get_target_repo()
    gh = get_client()
    repo = gh.get_repo(repo_full_name)
    commit = repo.get_commit(sha)
    for pr in commit.get_pulls():
        if pr.state != "open":
            continue
        await handle_dependabot_pr(pr.number, f"{event.external_id}-pr{pr.number}")
