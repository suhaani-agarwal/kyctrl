"""Deterministic rule engine for the Dependabot/Renovate merge decision.

Per kyctrl_extra_features.md Dimension 6: "Whether to merge a Dependabot PR
is NOT a judgment call — it's a policy check." This module is pure Python,
has no LLM involvement, and is the *only* thing that decides merge/hold.
`agents/dependabot.py` calls `evaluate()` first; the Claude Agent SDK is
only ever used afterward, to explain the decision in a PR comment and to
handle the "unknown" case (title didn't parse) as a genuinely ambiguous
situation worth a second look — never to overturn a merge/hold that this
module already reached deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from github.PullRequest import PullRequest

from src.config import DependabotPolicy
from src.tools.github_tools import pr_age_minutes, pr_checks_all_green, pr_labels

_BUMP_TITLE_RE = re.compile(r"bump\s+(?P<package>[\w./@-]+)\s+from\s+(?P<old>\S+)\s+to\s+(?P<new>\S+)", re.I)
_GROUP_TITLE_RE = re.compile(r"bump\s+the\s+(?P<group>[\w-]+)\s+group", re.I)
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


@dataclass
class MergeDecision:
    decision: str  # "merge" | "hold"
    rule: str  # short machine-readable rule id, goes in the audit log's decision_reason
    reason: str  # human-readable explanation


def semver_bump_type(old: str, new: str) -> str:
    old_m, new_m = _VERSION_RE.search(old), _VERSION_RE.search(new)
    if not old_m or not new_m:
        return "unknown"
    old_parts = tuple(int(x) for x in old_m.groups())
    new_parts = tuple(int(x) for x in new_m.groups())
    if new_parts[0] != old_parts[0]:
        return "major"
    if new_parts[1] != old_parts[1]:
        return "minor"
    if new_parts[2] != old_parts[2]:
        return "patch"
    return "unknown"


def parse_bump_title(title: str) -> tuple[str, str]:
    """Returns (package_or_group_name, bump_type). bump_type is "unknown"
    when the title doesn't give us enough to compute one deterministically
    — a grouped bump with no single from/to, or an unrecognized format."""
    m = _BUMP_TITLE_RE.search(title)
    if m:
        return m.group("package"), semver_bump_type(m.group("old"), m.group("new"))
    g = _GROUP_TITLE_RE.search(title)
    if g:
        return f"{g.group('group')} group", "unknown"
    return title.strip(), "unknown"


def is_excluded(package: str, excluded_packages: list[str]) -> bool:
    package_lower = package.lower()
    return any(pkg.lower() in package_lower for pkg in excluded_packages)


def evaluate(pr: PullRequest, policy: DependabotPolicy) -> MergeDecision:
    labels = pr_labels(pr)

    if policy.hold_label in labels:
        return MergeDecision("hold", "hold_label", f"Blocked by human-applied '{policy.hold_label}' label.")

    package, bump_type = parse_bump_title(pr.title)

    if bump_type == "unknown":
        return MergeDecision(
            "hold",
            "unparseable_bump",
            f"Could not determine the semver bump type from the PR title ({pr.title!r}). "
            "Treating as ambiguous rather than guessing.",
        )

    if bump_type == "major":
        return MergeDecision("hold", "major_bump", f"Major version bump of {package} — always reviewed by a human.")

    if policy.auto_merge == "none":
        return MergeDecision("hold", "policy_none", "dependabot.auto_merge is 'none' in the current config.")

    if policy.auto_merge == "patch_only" and bump_type != "patch":
        return MergeDecision(
            "hold", "policy_patch_only", f"{bump_type} bump, but dependabot.auto_merge is 'patch_only'."
        )

    if is_excluded(package, policy.excluded_packages):
        return MergeDecision("hold", "excluded_package", f"{package} is on the dependabot.excluded_packages list.")

    age = pr_age_minutes(pr)
    if age < policy.min_pr_age_minutes:
        return MergeDecision(
            "hold",
            "too_new",
            f"PR is {age:.1f} minutes old; minimum is {policy.min_pr_age_minutes} minutes so CI has a real chance to run.",
        )

    if not pr_checks_all_green(pr, policy.required_checks or None):
        return MergeDecision("hold", "ci_not_green", "Not all CI checks on the head commit are green yet.")

    return MergeDecision(
        "merge",
        "eligible",
        f"{bump_type} bump of {package}: not excluded, no hold label, all checks green, PR old enough.",
    )
