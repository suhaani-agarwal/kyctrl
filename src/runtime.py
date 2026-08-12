"""Process-wide wiring shared by every agent and by `main.py`.

Kept in one small module so agents stay focused on decision logic, not on
how the config/audit-db/GitHub-auth singletons get constructed.
"""

from __future__ import annotations

import os
from functools import lru_cache

from github import Github
from loguru import logger

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from src.audit import AuditWriter, get_engine
from src.config import AiMaintainerConfig, kill_switch_engaged, load_config
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


def get_target_repo() -> str:
    repo = os.environ.get("TARGET_REPO")
    if not repo:
        raise RuntimeError("TARGET_REPO env var not set (e.g. suhaani-agarwal/kyctrl-demo-target)")
    return repo


def get_client() -> Github:
    return get_github_auth().get_client(get_target_repo())


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


async def can_use_tool(tool_name: str, input_data: dict, context) -> PermissionResultAllow | PermissionResultDeny:
    """Passed as `ClaudeAgentOptions.can_use_tool` on every agent. Re-reads
    both kill switches before *every single tool call* — not just at
    agent start — so flipping `AI_MAINTAINER_ENABLED` mid-run genuinely
    interrupts the agent instead of only blocking the next one."""
    config = get_config()
    if kill_switch_engaged(config, get_repo_variable):
        logger.warning(f"can_use_tool: denying {tool_name} — kill switch engaged mid-run")
        return PermissionResultDeny(
            behavior="deny", message="AI Maintainer kill switch is engaged — no action taken.", interrupt=True
        )
    return PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)
