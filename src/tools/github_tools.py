"""GitHub actions, split into two layers on purpose:

1. **Plain functions** (`pr_labels`, `pr_checks_all_green`, `pr_age_minutes`,
   `pr_files`) — used directly by `agents/merge_policy.py`'s deterministic
   rule engine. The LLM never sees these; a merge decision is a policy
   check, not a judgment call (see kyctrl_extra_features.md Dimension 6).

2. **`@tool`-wrapped, per-event-scoped servers** (`build_pr_tool_server`,
   `build_issue_tool_server`) — what the Claude Agent SDK agent actually
   gets. Each server is built fresh per webhook event and *closes over* a
   single already-resolved PR or issue object, so the agent has no way to
   name a different PR/issue number and act on it — the scoping happens at
   construction time, not via a parameter the model could get wrong. This
   is "sandboxed... cannot touch things it wasn't given permission to
   touch" as code, not policy.

`set_repo_variable` (the kill-switch toggle) is deliberately a plain
function only, never wrapped as an agent `@tool` — the agent must never be
able to call the one action that could turn off its own kill switch.
"""

from __future__ import annotations

from datetime import datetime, timezone

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool
from github import Github, GithubException
from github.PullRequest import PullRequest
from loguru import logger


def _tool_error(action: str, exc: Exception) -> dict:
    """Every `@tool` body below wraps its GitHub call with this instead of
    letting the exception propagate. An uncaught `GithubException` (a
    missing scope, a 404, a rate limit) would otherwise crash the whole
    `query()` stream mid-run — see the 403 on `get_check_status` that a
    Checks-scope-less PAT triggered live. Returning `is_error` instead lets
    the model see what failed and decide what to do next (e.g. comment
    without CI info instead of the entire run dying)."""
    logger.warning(f"GitHub tool call failed ({action}): {exc}")
    return {"content": [{"type": "text", "text": f"{action} failed: {exc}"}], "is_error": True}


_STATUS_MARKER = "<!-- kyctrl:status-comment -->"


def _upsert_comment(get_comments, create_comment, body: str) -> None:
    """Keep exactly one bot status comment per PR/issue instead of stacking
    a new one on every re-run — the "sticky status comment" pattern most
    CI/preview bots use (a real webhook redelivery, a retried run, or
    someone re-triggering the workflow would otherwise spam the thread with
    near-duplicate comments, which is exactly what happened before this
    existed). `get_comments`/`create_comment` are bound methods so this
    works for both `PullRequest.get_issue_comments`/`create_issue_comment`
    and `Issue.get_comments`/`create_comment`."""
    marked_body = f"{body}\n\n{_STATUS_MARKER}"
    for comment in get_comments():
        if _STATUS_MARKER in (comment.body or ""):
            comment.edit(marked_body)
            return
    create_comment(marked_body)

# --- Plain functions: used by the deterministic rule engine, no LLM involved ---


def pr_labels(pr: PullRequest) -> set[str]:
    return {label.name for label in pr.get_labels()}


def pr_age_minutes(pr: PullRequest) -> float:
    created = pr.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 60


def pr_checks_all_green(pr: PullRequest, required_checks: list[str] | None = None) -> bool:
    """True only if every required status check (or the overall combined
    state, if no specific list is given) on the PR's head commit succeeded.
    Never guesses (no checks reported == not green, not "assume fine").

    Deliberately uses the Commit *Statuses* API (`get_combined_status`),
    not the Checks API (`get_check_runs`): fine-grained PATs can never be
    granted access to Checks at all — a permanent GitHub limitation, not a
    missing scope on any particular token (confirmed via GitHub staff:
    https://github.com/orgs/community/discussions/129512) — only GitHub
    Apps can call it. "Commit statuses" is a real, grantable fine-grained
    permission, so this is what actually works for PAT-based auth."""
    combined = pr.base.repo.get_commit(pr.head.sha).get_combined_status()
    if required_checks:
        by_context = {s.context: s.state for s in combined.statuses}
        return all(by_context.get(name) == "success" for name in required_checks)
    return combined.state == "success"


def pr_files(pr: PullRequest) -> list[dict]:
    return [
        {
            "filename": f.filename,
            "status": f.status,
            "additions": f.additions,
            "deletions": f.deletions,
            "patch": f.patch,
        }
        for f in pr.get_files()
    ]


def set_repo_variable(gh: Github, repo_full_name: str, name: str, value: str) -> None:
    """Never exposed to the agent as a tool — only the dashboard's
    kill-switch endpoint calls this directly."""
    repo = gh.get_repo(repo_full_name)
    try:
        var = repo.get_variable(name)
        var.edit(value)
    except Exception:
        repo.create_variable(name, value)
    logger.info(f"Set repo variable {name}={value} on {repo_full_name}")


# --- Agent-facing tool servers: scoped to exactly one PR or issue ---


def build_pr_tool_server(
    gh: Github, repo_full_name: str, pr_number: int, *, allow_merge: bool
) -> McpSdkServerConfig:
    """`allow_merge` gates whether `approve_and_merge_pr` exists in this
    server at all — when the deterministic rule engine in merge_policy.py
    hasn't already cleared the PR, the tool simply isn't offered. The
    model can't talk its way into a capability it wasn't given."""
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    @tool("get_pr_diff", "Get the changed files and diffs for this pull request", {})
    async def get_pr_diff(_args: dict) -> dict:
        try:
            return {"content": [{"type": "text", "text": str(pr_files(pr))}]}
        except GithubException as e:
            return _tool_error("get_pr_diff", e)

    @tool("get_check_status", "Get CI status for this pull request's head commit", {})
    async def get_check_status(_args: dict) -> dict:
        try:
            combined = pr.base.repo.get_commit(pr.head.sha).get_combined_status()
            statuses = [{"context": s.context, "state": s.state} for s in combined.statuses]
            return {"content": [{"type": "text", "text": str({"state": combined.state, "statuses": statuses})}]}
        except GithubException as e:
            return _tool_error("get_check_status", e)

    @tool("comment_on_pr", "Post a comment on this pull request", {"body": str})
    async def comment_on_pr(args: dict) -> dict:
        try:
            _upsert_comment(pr.get_issue_comments, pr.create_issue_comment, args["body"])
            return {"content": [{"type": "text", "text": "commented"}]}
        except GithubException as e:
            return _tool_error("comment_on_pr", e)

    tools = [get_pr_diff, get_check_status, comment_on_pr]

    if allow_merge:

        @tool(
            "approve_and_merge_pr",
            "Approve and squash-merge this pull request. Only call this after explaining why it is safe.",
            {"summary": str},
        )
        async def approve_and_merge_pr(args: dict) -> dict:
            try:
                pr.create_review(event="APPROVE", body=args["summary"])
                result = pr.merge(merge_method="squash", commit_message=args["summary"])
                return {"content": [{"type": "text", "text": f"merged={result.merged} sha={result.sha}"}]}
            except GithubException as e:
                return _tool_error("approve_and_merge_pr", e)

        tools.append(approve_and_merge_pr)

    return create_sdk_mcp_server(name="github-pr-tools", tools=tools)


def build_issue_tool_server(gh: Github, repo_full_name: str, issue_number: int) -> McpSdkServerConfig:
    """Only `comment_on_issue` — label changes for issue triage go through
    `transition_issue_state` (see `agents/issue_triage.py`'s `state_tools`
    server), which validates against the FSM before touching a label.
    There's deliberately no generic `add_label`/`remove_label` tool here:
    an issue's state label should only ever move through a validated
    transition, never as a free-form model choice."""
    repo = gh.get_repo(repo_full_name)
    issue = repo.get_issue(issue_number)

    @tool("comment_on_issue", "Post a comment on this issue", {"body": str})
    async def comment_on_issue(args: dict) -> dict:
        try:
            _upsert_comment(issue.get_comments, issue.create_comment, args["body"])
            return {"content": [{"type": "text", "text": "commented"}]}
        except GithubException as e:
            return _tool_error("comment_on_issue", e)

    return create_sdk_mcp_server(name="github-issue-tools", tools=[comment_on_issue])
