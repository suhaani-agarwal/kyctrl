"""Dependabot/Renovate auto-merge agent.

The merge/hold decision itself is made entirely by `merge_policy.evaluate`
before this module ever calls the Claude Agent SDK — see that module's
docstring and kyctrl_extra_features.md Dimension 6. Whether a hold needs
`needs-human-review` is *also* decided there (`MergeDecision.needs_human_review`)
and applied directly below, not left to the model — it was flip-flopping
across otherwise-identical runs when this was phrased as an LLM judgment
call ("does this genuinely require a human decision?"), which is exactly
the kind of inconsistency a deterministic rule shouldn't have. This agent's
only remaining job is to explain the decision in a PR comment, and (only
when the rule engine already cleared the PR) call the merge tool. The SDK
is never offered a tool it shouldn't have: `build_pr_tool_server(...,
allow_merge=)` only includes `approve_and_merge_pr` when the rule engine
said "merge", and never includes a generic `add_label` at all.
"""

from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions, SandboxNetworkConfig, SandboxSettings, query
from github import GithubException
from loguru import logger

from src.agents.merge_policy import evaluate
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

    options = ClaudeAgentOptions(
        system_prompt=skill,
        mcp_servers={"github": tool_server},
        # No built-in tools (Bash/Read/Write/...) and no `allowed_tools`
        # entries — an `allowed_tools` entry with no `(...)` specifier
        # auto-approves the tool *before* `can_use_tool` is consulted,
        # which would silently defeat the mid-run kill switch. Leaving
        # both empty means every tool call (there's only ever the small,
        # per-PR-scoped set in `tool_server` above) falls through to
        # `can_use_tool`, which is what actually enforces both "only
        # AI-Maintainer tools" and "kill switch can interrupt mid-run".
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
    """A `status` webhook fires when CI posts a commit status — see the
    Statuses-API step in the demo repo's CI workflow, and the comment on
    `pr_checks_all_green` for why Statuses rather than Checks. This exists
    because of a real race: the `pull_request` event above usually arrives
    and gets evaluated *before* CI finishes, so a PR that only turns green
    after that first evaluation would otherwise never be re-checked and
    would hold forever. Re-runs the same policy for every open PR whose
    head is this commit — almost always zero or one PR in practice."""
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
