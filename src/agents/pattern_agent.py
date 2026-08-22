"""Pattern Agent — kyctrl_extra_features.md Dimension 2: "runs on a weekly
schedule. It reads everything that happened ... and builds a structured
understanding of patterns ... It then files a single tracking issue linking
all three."

Triggered by `Event(source="cron", type="pattern-agent")` from
`POST /internal/cron/pattern-agent` (see main.py), which a scheduled GitHub
Actions workflow calls. Clustering is entirely deterministic
(`pattern_clustering.py`) — the LLM's only job, once a cluster already
exists, is drafting the tracking issue's natural-language summary.
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
from src.agents.pattern_clustering import ClusterableIssue, cluster_issues
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
    single_turn_prompt,
)
from src.terminal import stream_agent_run

WORKFLOW = "pattern_agent"

_SKILL = (
    "You are the Pattern Agent for Kyverno. You are given one or more clusters of issues "
    "a deterministic similarity check has already grouped together — you did not choose "
    "these groupings and should not second-guess them. Your only job: for each cluster, call "
    "`file_tracking_issue` once with a title and body that names the likely shared root cause "
    "in plain language and links every issue number in the cluster. Be specific about what's "
    "common across them (same component, same error, same recent change) — never a generic "
    "'these seem related' summary."
)


def _build_pattern_tool_server(repo):
    @tool(
        "file_tracking_issue",
        "File a new tracking issue linking a cluster of related issues.",
        {"title": str, "body": str},
    )
    async def file_tracking_issue(args: dict) -> dict:
        try:
            issue = repo.create_issue(title=args["title"], body=args["body"])
            return {"content": [{"type": "text", "text": f"filed tracking issue #{issue.number}"}]}
        except GithubException as e:
            logger.warning(f"GitHub tool call failed (file_tracking_issue): {e}")
            return {"content": [{"type": "text", "text": f"file_tracking_issue failed: {e}"}], "is_error": True}

    return create_sdk_mcp_server(name="pattern-tools", tools=[file_tracking_issue])


def _fetch_recent_issues(gh, repo_full_name: str, lookback_days: int) -> list[ClusterableIssue]:
    # PyGithub's search_issues wants a GitHub search-syntax query string, not
    # a relative-date DSL, so the cutoff date is computed explicitly here.
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    results = gh.search_issues(query=f"repo:{repo_full_name} is:issue created:>={since}")
    return [
        ClusterableIssue(
            number=i.number,
            title=i.title,
            body=i.body or "",
            labels={label.name for label in i.labels},
        )
        for i in results
    ]


async def handle_pattern_run(external_id: str) -> AuditEntry:
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

    repo_full_name = get_target_repo()
    gh = get_client()
    repo = gh.get_repo(repo_full_name)

    issues = _fetch_recent_issues(gh, repo_full_name, config.pattern_agent.lookback_days)
    clusters = cluster_issues(issues, min_cluster_size=config.pattern_agent.min_cluster_size)

    if not clusters:
        return audit.write(
            trigger_event="cron",
            external_id=external_id,
            workflow_name=WORKFLOW,
            agent_decision="no_clusters",
            decision_reason=f"{len(issues)} issues in the last {config.pattern_agent.lookback_days}d, no cluster reached min size {config.pattern_agent.min_cluster_size}",
            action_taken="none",
            action_result="success",
        )

    tool_server = _build_pattern_tool_server(repo)

    clusters_desc = "\n\n".join(
        f"Cluster {idx + 1}: " + ", ".join(f"#{i.number} {i.title!r}" for i in cluster)
        for idx, cluster in enumerate(clusters)
    )

    # Dimension 3 — see the matching comment in agents/dependabot.py. No
    # single entity to scope the prefetch query to here (a whole run's
    # worth of clusters, not one issue/PR), so this prefetches on the
    # cluster titles as a whole rather than skipping it.
    memory = get_memory_client()
    memory_facts = await memory_search(f"issue patterns: {clusters_desc}")
    mcp_servers = {"pattern": tool_server}
    if memory is not None:
        mcp_servers["memory"] = build_memory_tool_server(memory, default_limit=config.memory.search_top_k)

    options = ClaudeAgentOptions(
        system_prompt=_SKILL,
        mcp_servers=mcp_servers,
        tools=[],
        allowed_tools=[],
        can_use_tool=can_use_tool,
        max_turns=4 * len(clusters) + 2,
        sandbox=SandboxSettings(
            enabled=True,
            network=SandboxNetworkConfig(allowedDomains=["api.github.com", "api.anthropic.com"]),
        ),
    )

    prompt = (
        f"Deterministically-identified clusters this run:\n\n{clusters_desc}\n\n"
        f"Relevant memory (past pattern-agent runs): {memory_facts or 'none found'}\n\n"
        f"File one tracking issue per cluster."
    )

    result = await stream_agent_run(
        query(prompt=single_turn_prompt(prompt), options=options),
        title=f"Pattern Agent — {len(clusters)} cluster(s)",
    )

    action_result = "success"
    if result is None:
        action_result = "failed: no ResultMessage from agent run"
    elif result.is_error:
        action_result = f"failed: {result.subtype}"

    # One episode per cluster — this is Dimension 3's "Cross-Issue Pattern
    # Memory" made literal: each cluster's linked issue numbers give
    # Graphiti's extraction exactly what it needs to write RELATED_TO edges
    # between those Issue entities, not just a generic run summary.
    memory_refs: list[str] = []
    for idx, cluster in enumerate(clusters):
        issue_numbers = [i.number for i in cluster]
        refs = await memory_write(
            name=f"{repo_full_name}:pattern-cluster-{idx}",
            episode_body=(
                f"Pattern Agent linked issues {issue_numbers} as a cluster: "
                f"{', '.join(i.title for i in cluster)}. Filed via file_tracking_issue. "
                f"Agent action result: {action_result}."
            ),
            source_description=WORKFLOW,
        )
        if refs:
            memory_refs.extend(refs)

    return audit.write(
        trigger_event="cron",
        external_id=external_id,
        workflow_name=WORKFLOW,
        agent_decision=f"{len(clusters)}_clusters_found",
        decision_reason=clusters_desc,
        agent_reasoning_summary=result.result if result else None,
        action_taken="file_tracking_issue",
        action_result=action_result,
        can_be_reverted=True,
        revert_command="close the filed tracking issue(s)",
        total_cost_usd=result.total_cost_usd if result else None,
        duration_ms=result.duration_ms if result else None,
        memory_refs=json.dumps(memory_refs) if memory_refs else None,
    )


@register_handler("pattern-agent")
async def handle(event: Event) -> None:
    await handle_pattern_run(event.external_id)
