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
    """Deliberately NOT cached — config must be read fresh at the start of
    every run so a policy edit takes effect immediately, no redeploy."""
    return load_config(os.environ.get("AI_MAINTAINER_CONFIG_PATH", ".github/ai-maintainer.yaml"))


@lru_cache
def get_github_auth() -> GitHubAuth:
    return get_auth_from_env()


@lru_cache
def get_memory_client() -> Graphiti | None:
    """`None` (never an exception) is the "memory unavailable this run"
    signal callers check for — either `memory.enabled` is false, or
    `NEO4J_URI`/`NEO4J_PASSWORD` aren't set. Cached like `get_github_auth()`:
    the connection doesn't change mid-process the way the kill switch does."""
    if not get_config().memory.enabled:
        return None
    uri = os.environ.get("NEO4J_URI")
    password = os.environ.get("NEO4J_PASSWORD")
    if not uri or not password:
        logger.warning("memory.enabled is true but NEO4J_URI/NEO4J_PASSWORD unset — memory disabled this run")
        return None
    return build_graphiti_client(uri, os.environ.get("NEO4J_USERNAME", "neo4j"), password)


def rate_limit_exceeded(config: AiMaintainerConfig, workflow: str) -> bool:
    """True if `workflow` has already made >= its configured rate limit of
    actions in the trailing hour, per the audit log. No configured limit
    means don't throttle. Only counts real actions (`action_taken != "none"`)
    — a storm of kill-switch/disabled-workflow skips doesn't count as the
    runaway behavior this guards against."""
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
    of a `Github` client (GitHub Discussions has no REST API, so PyGithub
    can't front that call)."""
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
# sets `tools=[]` and leaves `allowed_tools` empty so every call reaches
# `can_use_tool` below — an `allowed_tools` entry would auto-approve and
# skip this callback, silently defeating the mid-run kill switch.
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
    """Passed as `ClaudeAgentOptions.can_use_tool` on every agent. Re-checks
    both kill switches before every tool call (not just at agent start) so
    flipping the switch mid-run actually interrupts it, and enforces the
    tool-name allow-list above."""
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
    """The SDK requires streaming-mode input (`AsyncIterable[dict]`) whenever
    `can_use_tool` is set — a plain `str` prompt raises at run time. This
    wraps a single-turn prompt as the one-item async stream it wants."""
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }
