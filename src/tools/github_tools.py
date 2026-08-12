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
from github import Github
from github.PullRequest import PullRequest
from loguru import logger

# --- Plain functions: used by the deterministic rule engine, no LLM involved ---


def pr_labels(pr: PullRequest) -> set[str]:
    return {label.name for label in pr.get_labels()}


def pr_age_minutes(pr: PullRequest) -> float:
    created = pr.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 60


def pr_checks_all_green(pr: PullRequest, required_checks: list[str] | None = None) -> bool:
    """True only if every required check (or every reported check, if the
    list is empty) on the PR's head commit succeeded. Never guesses."""
    commit = pr.base.repo.get_commit(pr.head.sha)
    check_runs = list(commit.get_check_runs())
    if not check_runs:
        return False
    by_name = {run.name: run.conclusion for run in check_runs}
    names_to_check = required_checks or list(by_name.keys())
    return all(by_name.get(name) == "success" for name in names_to_check)


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
        return {"content": [{"type": "text", "text": str(pr_files(pr))}]}

    @tool("get_check_status", "Get CI check results for this pull request's head commit", {})
    async def get_check_status(_args: dict) -> dict:
        commit = pr.base.repo.get_commit(pr.head.sha)
        runs = [{"name": r.name, "conclusion": r.conclusion} for r in commit.get_check_runs()]
        return {"content": [{"type": "text", "text": str(runs)}]}

    @tool("comment_on_pr", "Post a comment on this pull request", {"body": str})
    async def comment_on_pr(args: dict) -> dict:
        pr.create_issue_comment(args["body"])
        return {"content": [{"type": "text", "text": "commented"}]}

    @tool("add_label", "Add a label to this pull request", {"label": str})
    async def add_label(args: dict) -> dict:
        pr.add_to_labels(args["label"])
        return {"content": [{"type": "text", "text": f"labeled {args['label']}"}]}

    tools = [get_pr_diff, get_check_status, comment_on_pr, add_label]

    if allow_merge:

        @tool(
            "approve_and_merge_pr",
            "Approve and squash-merge this pull request. Only call this after explaining why it is safe.",
            {"summary": str},
        )
        async def approve_and_merge_pr(args: dict) -> dict:
            pr.create_review(event="APPROVE", body=args["summary"])
            result = pr.merge(merge_method="squash", commit_message=args["summary"])
            return {"content": [{"type": "text", "text": f"merged={result.merged} sha={result.sha}"}]}

        tools.append(approve_and_merge_pr)

    return create_sdk_mcp_server(name="github-pr-tools", tools=tools)


def build_issue_tool_server(gh: Github, repo_full_name: str, issue_number: int) -> McpSdkServerConfig:
    repo = gh.get_repo(repo_full_name)
    issue = repo.get_issue(issue_number)

    @tool("comment_on_issue", "Post a comment on this issue", {"body": str})
    async def comment_on_issue(args: dict) -> dict:
        issue.create_comment(args["body"])
        return {"content": [{"type": "text", "text": "commented"}]}

    @tool("add_label", "Add a label to this issue", {"label": str})
    async def add_label(args: dict) -> dict:
        issue.add_to_labels(args["label"])
        return {"content": [{"type": "text", "text": f"labeled {args['label']}"}]}

    @tool("remove_label", "Remove a label from this issue", {"label": str})
    async def remove_label(args: dict) -> dict:
        issue.remove_from_labels(args["label"])
        return {"content": [{"type": "text", "text": f"removed {args['label']}"}]}

    return create_sdk_mcp_server(
        name="github-issue-tools", tools=[comment_on_issue, add_label, remove_label]
    )
