"""Coach Agent — kyctrl_extra_features.md Dimension 2: "analyzes contributor
PRs and produces targeted, encouraging feedback on code style, test coverage
gaps, and Kyverno conventions. Not a code reviewer — a mentor."

Registers independently on `pull_request` (see events.py's fan-out) so it
coexists with `dependabot.py` on the same event type without either module
knowing about the other — the two are mutually exclusive on any given PR by
construction: `is_bot_author` decides which one actually acts.

Reuses `build_pr_tool_server(..., allow_merge=False)` as-is rather than
inventing a parallel tool server — Coach never needs (and is never offered)
`approve_and_merge_pr`, so the existing "capability doesn't exist" pattern
already gives it exactly the right scope: read the diff, read CI status,
post one comment.

The one thing decided in Python, not by the model: whether the diff touches
a `safe_boundaries.restricted_paths` path. That's stated as fact in the
prompt, not left for the model to notice or miss.
"""

from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions, SandboxNetworkConfig, SandboxSettings, query
from loguru import logger

from src.agents._shared import is_bot_author
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
from src.tools.github_tools import build_pr_tool_server, pr_files

WORKFLOW = "coach_agent"


def _touches_restricted_paths(files: list[dict], restricted_paths: list[str]) -> list[str]:
    touched = []
    for f in files:
        filename = f["filename"]
        for restricted in restricted_paths:
            prefix = restricted.rstrip("/")
            if filename == prefix or filename.startswith(prefix + "/"):
                touched.append(restricted)
                break
    return touched


async def handle_coach_pr(pr_number: int, external_id: str, parent_run_id: int | None = None) -> AuditEntry:
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
            parent_run_id=parent_run_id,
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
            parent_run_id=parent_run_id,
        )

    repo_full_name = get_target_repo()
    gh = get_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    if config.coach_agent.exclude_bot_authors and is_bot_author(pr.user.login, config):
        logger.info(f"PR #{pr_number} author {pr.user.login!r} is a dependency bot — Coach Agent skips it")
        return audit.write(
            trigger_event="pull_request",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="skipped",
            decision_reason=f"author {pr.user.login!r} is a configured bot account",
            action_taken="none",
            action_result="skipped: not a human contributor PR",
            parent_run_id=parent_run_id,
        )

    files = pr_files(pr)
    restricted_hits = _touches_restricted_paths(files, config.safe_boundaries.restricted_paths)

    tool_server = build_pr_tool_server(gh, repo_full_name, pr_number, allow_merge=False)
    skill = load_skill("coach")

    options = ClaudeAgentOptions(
        system_prompt=skill,
        mcp_servers={"coach": tool_server},
        tools=[],
        allowed_tools=[],
        can_use_tool=can_use_tool,
        max_turns=6,
        sandbox=SandboxSettings(
            enabled=True,
            network=SandboxNetworkConfig(allowedDomains=["api.github.com", "api.anthropic.com"]),
        ),
    )

    restricted_note = (
        f"This diff touches path(s) requiring human review regardless of content: {restricted_hits}. "
        f"Mention this plainly, before your style feedback — the contributor should know a maintainer "
        f"will need to look at this part specifically."
        if restricted_hits
        else "This diff does not touch any restricted paths."
    )

    prompt = (
        f"PR #{pr_number} by {pr.user.login!r}: {pr.title!r}\n"
        f"{restricted_note}\n\n"
        f"Call `get_pr_diff` to see the changed files, then post one encouraging, specific comment "
        f"via `comment_on_pr` — mentor tone per the skill doc, not a generic checklist. Point out "
        f"one or two concrete things (style, test coverage, a Kyverno convention) and link the "
        f"relevant AGENTS.md section for the package(s) touched, if the skill doc names one."
    )

    result = await stream_agent_run(
        query(prompt=single_turn_prompt(prompt), options=options),
        title=f"Coach Agent — PR #{pr_number}",
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
        agent_decision="commented",
        decision_reason=f"restricted_paths_touched={restricted_hits}" if restricted_hits else "no restricted paths",
        agent_reasoning_summary=result.result if result else None,
        action_taken="comment_on_pr",
        action_result=action_result,
        can_be_reverted=True,
        revert_command="delete the bot's comment",
        parent_run_id=parent_run_id,
        total_cost_usd=result.total_cost_usd if result else None,
        duration_ms=result.duration_ms if result else None,
    )


@register_handler("pull_request")
async def handle(event: Event) -> None:
    if event.action not in ("opened", "reopened"):
        return
    pr_number = event.payload.get("number") or event.payload.get("pull_request", {}).get("number")
    if pr_number is None:
        logger.warning("pull_request event missing PR number, skipping (coach)")
        return
    await handle_coach_pr(pr_number, f"{event.external_id}-coach")
