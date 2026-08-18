"""Process-wide wiring shared by every agent and by `main.py`.

Kept in one small module so agents stay focused on decision logic, not on
how the config/audit-db/GitHub-auth singletons get constructed.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from github import Github
from graphiti_core import Graphiti
from loguru import logger
from sqlmodel import Session, func, select

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from src.audit import AuditEntry, AuditWriter, get_engine
from src.config import AiMaintainerConfig, kill_switch_engaged, load_config
from src.memory import build_graphiti_client
from src.tools.github_auth import GitHubAuth, get_auth_from_env


@lru_cache
def get_audit_writer() -> AuditWriter:
    return AuditWriter(get_engine(os.environ.get("AUDIT_DB_PATH", "audit.sqlite3")))


def get_config() -> AiMaintainerConfig:
    """Deliberately NOT cached — §5.4/§Hard-Constraints: config is read
    remotely at the start of every run, never stale. In this prototype
    "remotely" is a local file (or, once deployed, the same file fetched
    from the repo via the GitHub API); either way, every call re-reads."""
    return load_config(os.environ.get("AI_MAINTAINER_CONFIG_PATH", ".github/ai-maintainer.yaml"))


@lru_cache
def get_github_auth() -> GitHubAuth:
    return get_auth_from_env()


@lru_cache
def get_memory_client() -> Graphiti | None:
    """`None` — not an exception — is the "memory unavailable this run"
    signal every caller (`src/agents/_shared.py`'s `memory_search`/
    `memory_write`) checks for, same shape as `get_repo_variable` treating
    any failure as "unset" rather than raising. Two independent reasons to
    return `None`: `memory.enabled: false` in config (the default — see
    `MemoryPolicy`), or `NEO4J_URI`/`NEO4J_PASSWORD` simply not set (a dev
    environment that never ran `docker compose up neo4j`). `@lru_cache`,
    not re-read every call like `get_config()` — env vars and the
    Graphiti/Neo4j connection don't change mid-process the way the kill
    switch needs to; this mirrors `get_github_auth()`'s tradeoff, not
    `get_config()`'s. `Graphiti(...)`'s own constructor is synchronous (only
    `build_indices_and_constraints()`/`add_episode()`/`search()` are async),
    so `@lru_cache` is safe here — no `doc_retriever.get_rag()`-style manual
    singleton needed."""
    if not get_config().memory.enabled:
        return None
    uri = os.environ.get("NEO4J_URI")
    password = os.environ.get("NEO4J_PASSWORD")
    if not uri or not password:
        logger.warning("memory.enabled is true but NEO4J_URI/NEO4J_PASSWORD unset — memory disabled this run")
        return None
    return build_graphiti_client(uri, os.environ.get("NEO4J_USERNAME", "neo4j"), password)


def rate_limit_exceeded(config: AiMaintainerConfig, workflow: str) -> bool:
    """True if `workflow` has already made >= its configured
    `rate_limits[workflow]` actions in the trailing hour, per the audit log
    — the existing AuditWriter/sqlite is the natural source of truth here,
    since every run already writes one row with `workflow_name` and
    `timestamp`. No configured limit for a workflow means "don't throttle":
    absence in `rate_limits` is the safe default, the mirror image of
    `workflow_enabled`'s "absence means don't run" (that dict's absence has
    to default to the *safer* direction in each case, and the safer
    direction is opposite depending on which dict it is). Only counts rows
    with `action_taken != "none"` — a storm of instant kill-switch/
    disabled-workflow skips isn't the "runaway behavior" the config's
    `rate_limits` comment is guarding against (e.g. a webhook replay
    storm); a storm of real decision attempts is."""
    limit = config.rate_limits.get(workflow)
    if limit is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    with Session(get_audit_writer().engine) as session:
        count = session.exec(
            select(func.count())
            .select_from(AuditEntry)
            .where(
                AuditEntry.workflow_name == workflow,
                AuditEntry.timestamp >= cutoff,
                AuditEntry.action_taken != "none",
            )
        ).one()
    return count >= limit


def get_target_repo() -> str:
    repo = os.environ.get("TARGET_REPO")
    if not repo:
        raise RuntimeError("TARGET_REPO env var not set (e.g. suhaani-agarwal/kyctrl-demo-target)")
    return repo


def get_client() -> Github:
    return get_github_auth().get_client(get_target_repo())


def get_github_token() -> str:
    """Raw bearer token, for the rare caller that needs one directly instead
    of a `Github` client (currently only `discussion_tools.add_discussion_comment`
    — GitHub Discussions has no REST API, so PyGithub can't front the call)."""
    return get_github_auth().get_token(get_target_repo())


def get_repo_variable(name: str) -> str | None:
    """The live half of the two-layer kill switch — see
    `config.kill_switch_engaged`. Any failure (repo has no such variable,
    auth hiccup) is treated as "no override", not as "engaged" — the
    config-file switch still applies underneath."""
    try:
        repo = get_client().get_repo(get_target_repo())
        return repo.get_variable(name).value
    except Exception as e:
        logger.debug(f"get_repo_variable({name}) failed, treating as unset: {e}")
        return None


# The only tool namespaces any agent is ever allowed to call. Every agent
# sets `tools=[]` (no built-in Bash/Read/Write/WebFetch/...) and leaves
# `allowed_tools` empty, so *every* tool call — not just the ones an
# allow-rule doesn't already auto-approve — reaches this callback (an
# `allowed_tools` entry with no `(...)` specifier auto-approves and skips
# this callback entirely, which would silently defeat the mid-run kill
# switch below; see `CanUseToolShadowedWarning`). This function is
# therefore the *only* place tool access is decided, so it has to enforce
# both scoping rules itself: kill switch, and "only tools we built."
_ALLOWED_TOOL_PREFIXES = (
    "mcp__github__",
    "mcp__state__",
    "mcp__qa__",
    "mcp__pattern__",
    "mcp__coach__",
    "mcp__security__",
    "mcp__memory__",
)


async def can_use_tool(tool_name: str, input_data: dict, context) -> PermissionResultAllow | PermissionResultDeny:
    """Passed as `ClaudeAgentOptions.can_use_tool` on every agent. Re-reads
    both kill switches before *every single tool call* — not just at
    agent start — so flipping `AI_MAINTAINER_ENABLED` mid-run genuinely
    interrupts the agent instead of only blocking the next one. Also
    enforces the tool-name allow-list described above, so a model that
    somehow gets offered an unexpected tool (a built-in, a future MCP
    server) can't have it rubber-stamped just because the kill switch
    happens to be off."""
    config = get_config()
    if kill_switch_engaged(config, get_repo_variable):
        logger.warning(f"can_use_tool: denying {tool_name} — kill switch engaged mid-run")
        return PermissionResultDeny(
            behavior="deny", message="AI Maintainer kill switch is engaged — no action taken.", interrupt=True
        )
    if not tool_name.startswith(_ALLOWED_TOOL_PREFIXES):
        logger.warning(f"can_use_tool: denying {tool_name} — not in the allowed tool namespaces")
        return PermissionResultDeny(
            behavior="deny", message=f"{tool_name} is not an AI Maintainer tool.", interrupt=False
        )
    return PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)


async def single_turn_prompt(text: str) -> AsyncIterator[dict]:
    """Every agent sets `can_use_tool` (above), and the installed SDK
    requires streaming-mode input — an `AsyncIterable[dict]` — whenever
    `can_use_tool` is set; a plain `str` prompt now raises `ValueError`
    at run time (see `InternalClient._process_query_inner`). This wraps a
    single-turn prompt as the one-item async stream the SDK wants, so
    callers still get `query()`'s stateless one-shot semantics — just
    with the message shape the SDK requires."""
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }
