"""Type-safe loading of `.github/ai-maintainer.yaml`, the single source of
truth for agent behavior. Every agent run starts by calling `load_config()`
and `kill_switch_engaged()` — a misconfigured file fails loudly here rather
than causing silent misbehavior later.

Two independent kill switches are modeled on purpose: `enabled` in the
committed config file, and the `AI_MAINTAINER_ENABLED` repo variable
(instant, no PR needed — wins if the two disagree, see
`kill_switch_engaged`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml
from loguru import logger
from pydantic import BaseModel, Field, field_validator


class DependabotPolicy(BaseModel):
    auto_merge: str = Field(default="patch_and_minor")
    bot_usernames: list[str] = Field(default_factory=lambda: ["dependabot[bot]", "renovate[bot]"])
    excluded_packages: list[str] = Field(default_factory=list)
    min_pr_age_minutes: int = 5
    hold_label: str = "hold"
    needs_review_label: str = "needs-human-review"
    required_checks: list[str] = Field(default_factory=list)
    # Off by default so upgrading doesn't silently start holding PRs that
    # merge fine today. No API key needed (src/tools/osv_tools.py) to enable.
    osv_check_enabled: bool = False

    @field_validator("auto_merge")
    @classmethod
    def _valid_auto_merge(cls, v: str) -> str:
        allowed = {"patch_and_minor", "patch_only", "none"}
        if v not in allowed:
            raise ValueError(f"dependabot.auto_merge must be one of {allowed}, got {v!r}")
        return v


class IssueTriagePolicy(BaseModel):
    labels: dict[str, str] = Field(default_factory=dict)
    required_bug_fields: list[str] = Field(default_factory=list)
    webhook_only_fields: list[str] = Field(default_factory=list)
    exclusion_labels: list[str] = Field(default_factory=list)


class SafeBoundaries(BaseModel):
    restricted_paths: list[str] = Field(default_factory=list)
    autonomous_paths: list[str] = Field(default_factory=list)


class QaAssistantEscalation(BaseModel):
    slack_channel: str = "kyverno-maintainers"
    github_maintainer_logins: list[str] = Field(default_factory=list)


class QaAssistantPolicy(BaseModel):
    # Minimum self-reported confidence before an answer is posted publicly —
    # a deterministic check (qa_assistant.py), never the LLM's own call.
    confidence_threshold: str = "high"
    max_search_results: int = 5
    default_kyverno_version: str = "latest"
    slack_channels: list[str] = Field(default_factory=lambda: ["kyverno"])
    discussion_categories: list[str] = Field(default_factory=list)
    escalation: QaAssistantEscalation = Field(default_factory=QaAssistantEscalation)

    @field_validator("confidence_threshold")
    @classmethod
    def _valid_confidence(cls, v: str) -> str:
        allowed = {"high", "medium", "low"}
        if v not in allowed:
            raise ValueError(f"qa_assistant.confidence_threshold must be one of {allowed}, got {v!r}")
        return v


class PatternAgentPolicy(BaseModel):
    lookback_days: int = 7
    min_cluster_size: int = 2


class CoachAgentPolicy(BaseModel):
    exclude_bot_authors: bool = True


class SecurityAgentPolicy(BaseModel):
    trigger_labels: list[str] = Field(default_factory=lambda: ["security"])
    private_slack_channel: str = "kyverno-security-private"


class ReproductionAgentPolicy(BaseModel):
    workflow_file: str = "reproduce-issue.yaml"


class MemoryPolicy(BaseModel):
    # Off by default until its infra (Neo4j + VOYAGE_API_KEY) is wired up.
    enabled: bool = False
    # Default number of facts a memory_search call returns (src/agents/_shared.py).
    search_top_k: int = 5


class AiMaintainerConfig(BaseModel):
    enabled: bool = True
    workflows: dict[str, bool] = Field(default_factory=dict)
    rate_limits: dict[str, int] = Field(default_factory=dict)
    dependabot: DependabotPolicy = Field(default_factory=DependabotPolicy)
    issue_triage: IssueTriagePolicy = Field(default_factory=IssueTriagePolicy)
    safe_boundaries: SafeBoundaries = Field(default_factory=SafeBoundaries)
    qa_assistant: QaAssistantPolicy = Field(default_factory=QaAssistantPolicy)
    pattern_agent: PatternAgentPolicy = Field(default_factory=PatternAgentPolicy)
    coach_agent: CoachAgentPolicy = Field(default_factory=CoachAgentPolicy)
    security_agent: SecurityAgentPolicy = Field(default_factory=SecurityAgentPolicy)
    reproduction_agent: ReproductionAgentPolicy = Field(default_factory=ReproductionAgentPolicy)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)

    def workflow_enabled(self, name: str) -> bool:
        """A workflow only runs if both the global switch and its own switch are on."""
        return self.enabled and self.workflows.get(name, False)


def load_config(path: str | Path = ".github/ai-maintainer.yaml") -> AiMaintainerConfig:
    """Load and validate the config file. Raises on malformed YAML/schema —
    fail fast and loud rather than let an agent misbehave on bad config."""
    path = Path(path)
    if not path.exists():
        logger.warning(f"No config file at {path}, falling back to safe defaults (all workflows off)")
        return AiMaintainerConfig(workflows={})
    raw = yaml.safe_load(path.read_text()) or {}
    config = AiMaintainerConfig.model_validate(raw)
    logger.info(f"Loaded config from {path}: enabled={config.enabled}, workflows={config.workflows}")
    return config


# `GetRepoVariable` lets callers inject however they fetch the live GitHub
# Actions repo variable (real GitHub API call in production, a stub in
# tests) without this module depending on `github_tools` directly.
GetRepoVariable = Callable[[str], str | None]


def kill_switch_engaged(config: AiMaintainerConfig, get_repo_variable: GetRepoVariable) -> bool:
    """True if the agent must not act at all. Checks BOTH kill switches;
    either one being "off" stops everything. The repo variable is the fast
    path (no PR/redeploy needed) so it's checked first."""
    live_value = get_repo_variable("AI_MAINTAINER_ENABLED")
    if live_value is not None and live_value.strip().lower() == "false":
        logger.warning("Kill switch engaged via AI_MAINTAINER_ENABLED repo variable — no action taken")
        return True
    if not config.enabled:
        logger.warning("Kill switch engaged via .github/ai-maintainer.yaml `enabled: false` — no action taken")
        return True
    return False
