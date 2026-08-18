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

import yaml
from github import GithubException
from github.PullRequest import PullRequest

from src.config import DependabotPolicy
from src.tools.github_tools import pr_age_minutes, pr_checks_all_green, pr_labels
from src.tools.osv_tools import OsvCheckUnavailable, check_package_vulnerabilities, infer_ecosystem

_BUMP_TITLE_RE = re.compile(r"bump\s+(?P<package>[\w./@-]+)\s+from\s+(?P<old>\S+)\s+to\s+(?P<new>\S+)", re.I)
_GROUP_TITLE_RE = re.compile(r"bump\s+the\s+(?P<group>[\w-]+)\s+group", re.I)
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

# Dependabot embeds a structured `updated-dependencies` YAML block as a git
# trailer in every commit it authors — the same ground truth the
# `dependabot/fetch-metadata` GitHub Action reads and re-exposes as PR
# labels/outputs. Reading it directly here means merge_policy.py never
# needs that Action (or a label-stamping workflow racing this service's own
# webhook) to get exact per-package semver/dependency-type data, including
# for grouped PRs — see resolve_bump()/group_bump_type() below, and
# parse_bump_title()'s docstring for why that function still exists as a
# fallback. Terminated by a bare "..." YAML end-of-document marker, not
# end-of-string — a `Signed-off-by:` line always follows it.
_TRAILER_RE = re.compile(r"^---\nupdated-dependencies:\n(?P<body>.*?)\n\.\.\.\s*$", re.S | re.M)
# The "Bumps X from A to B." / "Updates `X` from A to B" prose line(s) in the
# same commit message — the trailer's `update-type` doesn't carry old/new
# version numbers, only the bump *kind*, so this is the one extra thing we
# need from the message body to know what version is actually being merged.
_BUMPS_LINE_RE = re.compile(
    r"(?:Bumps?|Updates?)\s+`?(?P<name>[\w./@-]+)`?\s+from\s+(?P<old>\S+)\s+to\s+(?P<new>\S+)", re.I
)
_UPDATE_TYPE_TO_BUMP = {
    "version-update:semver-patch": "patch",
    "version-update:semver-minor": "minor",
    "version-update:semver-major": "major",
}
_BUMP_SEVERITY = {"patch": 0, "minor": 1, "major": 2, "unknown": 3}


@dataclass
class DependencyUpdate:
    """One entry from a Dependabot commit's `updated-dependencies` trailer."""

    name: str
    dependency_type: str  # "direct:production" | "direct:development" | "indirect" | "unknown"
    update_type: str  # raw trailer value, e.g. "version-update:semver-patch"
    bump_type: str  # normalized: "patch" | "minor" | "major" | "unknown"
    new_version: str | None = None  # parsed from the message's prose "Bumps/Updates ... to X" line


@dataclass
class MergeDecision:
    decision: str  # "merge" | "hold"
    rule: str  # short machine-readable rule id, goes in the audit log's decision_reason
    reason: str  # human-readable explanation
    needs_human_review: bool = False  # see evaluate() — which hold rules actually need a maintainer's judgment


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
    """Returns (package_or_group_name, bump_type), by regex on the PR
    **title** alone. This is the fallback path — resolve_bump() below
    prefers the structured commit trailer and only falls through to this
    when no commit on the PR has one (e.g. Renovate, which doesn't write
    Dependabot's trailer format). bump_type is "unknown" when the title
    doesn't give us enough to compute one deterministically — a grouped
    bump has no single from/to in its title, so it's always "unknown" here
    (see group_bump_type() for how a grouped trailer actually resolves this)."""
    m = _BUMP_TITLE_RE.search(title)
    if m:
        return m.group("package"), semver_bump_type(m.group("old"), m.group("new"))
    g = _GROUP_TITLE_RE.search(title)
    if g:
        return f"{g.group('group')} group", "unknown"
    return title.strip(), "unknown"


def parse_commit_trailer(message: str) -> list[DependencyUpdate]:
    """Extracts Dependabot's `updated-dependencies` trailer from one commit
    message. Returns [] on anything malformed or absent — never guesses;
    callers fall back to title parsing (see resolve_bump()) rather than
    treat an empty list as "zero dependencies changed"."""
    m = _TRAILER_RE.search(message)
    if not m:
        return []
    try:
        parsed = yaml.safe_load(m.group("body"))
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, list):
        return []

    # rstrip(".") because the single-package prose line ends the sentence
    # with a period directly after the version ("Bumps foo ... to 1.2.3.");
    # the grouped "Updates `foo` from ... to 1.2.3" line has no such suffix.
    new_versions = {bm.group("name"): bm.group("new").rstrip(".") for bm in _BUMPS_LINE_RE.finditer(message)}

    updates = []
    for entry in parsed:
        if not isinstance(entry, dict) or "dependency-name" not in entry:
            continue
        name = entry["dependency-name"]
        update_type = entry.get("update-type", "unknown")
        updates.append(
            DependencyUpdate(
                name=name,
                dependency_type=entry.get("dependency-type", "unknown"),
                update_type=update_type,
                bump_type=_UPDATE_TYPE_TO_BUMP.get(update_type, "unknown"),
                new_version=new_versions.get(name),
            )
        )
    return updates


def parse_pr_dependency_updates(pr: PullRequest) -> list[DependencyUpdate]:
    """Reads every commit on the PR (usually one, but a rebase can leave
    more) and parses each one's trailer, de-duped by dependency-name —
    last-seen wins, since a force-push replaces history and a later
    commit's trailer for the same package supersedes an earlier one."""
    by_name: dict[str, DependencyUpdate] = {}
    for commit in pr.get_commits():
        for update in parse_commit_trailer(commit.commit.message):
            by_name[update.name] = update
    return list(by_name.values())


def group_bump_type(updates: list[DependencyUpdate]) -> str:
    """The "evaluate the highest semver bump type across the whole group"
    behavior skills/kyverno/dependabot-policy.md already documents —
    actually implemented, using real per-package data instead of guessing
    from a title that only ever names the group. Any single "unknown" entry
    makes the whole result "unknown": never silently drop a bad entry just
    because its siblings parsed fine."""
    if not updates:
        return "unknown"
    return max((u.bump_type for u in updates), key=lambda b: _BUMP_SEVERITY.get(b, 3))


def _bump_from_updates(updates: list[DependencyUpdate]) -> tuple[str, str]:
    bump_type = group_bump_type(updates)
    if len(updates) == 1:
        return updates[0].name, bump_type
    return f"{len(updates)}-package group ({', '.join(u.name for u in updates)})", bump_type


def resolve_bump(pr: PullRequest) -> tuple[str, str]:
    """Package/group label + bump type for this PR, preferring the
    structured commit trailer (correct for both single and grouped
    Dependabot PRs) and falling back to parse_bump_title() only when no
    commit has a parseable trailer at all."""
    updates = parse_pr_dependency_updates(pr)
    if updates:
        return _bump_from_updates(updates)
    return parse_bump_title(pr.title)


def is_excluded(package: str, excluded_packages: list[str]) -> bool:
    package_lower = package.lower()
    return any(pkg.lower() in package_lower for pkg in excluded_packages)


def _first_excluded(package: str, updates: list[DependencyUpdate], excluded_packages: list[str]) -> str | None:
    """Checks each individual dependency from a grouped trailer against the
    exclusion list, not just the combined group label — a group PR bumping
    an ordinary module alongside an excluded one (e.g. k8s.io/api next to
    the excluded k8s.io/client-go) must still hold, rather than depending on
    substring luck against the joined label string."""
    names = [u.name for u in updates] if updates else [package]
    return next((name for name in names if is_excluded(name, excluded_packages)), None)


def evaluate(pr: PullRequest, policy: DependabotPolicy) -> MergeDecision:
    labels = pr_labels(pr)

    if policy.hold_label in labels:
        return MergeDecision("hold", "hold_label", f"Blocked by human-applied '{policy.hold_label}' label.")

    updates = parse_pr_dependency_updates(pr)
    package, bump_type = _bump_from_updates(updates) if updates else parse_bump_title(pr.title)

    if bump_type == "unknown":
        source = (
            f"one or more entries in the commit trailer had an unrecognized update-type ({package})"
            if updates
            else f"the PR title didn't parse ({pr.title!r})"
        )
        return MergeDecision(
            "hold",
            "unparseable_bump",
            f"Could not determine the semver bump type: {source}. Treating as ambiguous rather than guessing.",
            needs_human_review=True,
        )

    if bump_type == "major":
        return MergeDecision(
            "hold",
            "major_bump",
            f"Major version bump of {package} — always reviewed by a human.",
            needs_human_review=True,
        )

    if policy.auto_merge == "none":
        return MergeDecision("hold", "policy_none", "dependabot.auto_merge is 'none' in the current config.")

    if policy.auto_merge == "patch_only" and bump_type != "patch":
        return MergeDecision(
            "hold", "policy_patch_only", f"{bump_type} bump, but dependabot.auto_merge is 'patch_only'."
        )

    excluded = _first_excluded(package, updates, policy.excluded_packages)
    if excluded:
        return MergeDecision(
            "hold",
            "excluded_package",
            f"{excluded} is on the dependabot.excluded_packages list.",
            needs_human_review=True,
        )

    age = pr_age_minutes(pr)
    if age < policy.min_pr_age_minutes:
        return MergeDecision(
            "hold",
            "too_new",
            f"PR is {age:.1f} minutes old; minimum is {policy.min_pr_age_minutes} minutes so CI has a real chance to run.",
        )

    try:
        checks_green = pr_checks_all_green(pr, policy.required_checks or None)
    except GithubException as e:
        # Same "never guess" philosophy as the unparseable-title case above:
        # if we can't even determine CI status (missing token scope, a
        # transient API failure), that's ambiguous, not green — hold,
        # don't crash the whole run and don't merge on a guess.
        return MergeDecision(
            "hold", "checks_unavailable", f"Could not read CI check status ({e}); treating as not verified."
        )

    if not checks_green:
        return MergeDecision("hold", "ci_not_green", "Not all CI checks on the head commit are green yet.")

    if policy.osv_check_enabled and updates:
        # Only runs when the commit trailer parsed (see resolve_bump()) —
        # that's the only source of the exact per-package new version OSV
        # needs to query. A bot that doesn't write Dependabot's trailer
        # format (Renovate) simply isn't covered by this check yet, same as
        # group_bump_type() above; that's a documented gap, not a guess.
        for update in updates:
            if not update.new_version:
                return MergeDecision(
                    "hold",
                    "osv_version_unknown",
                    f"Could not determine the new version of {update.name} to check against OSV.dev; "
                    "treating as not verified.",
                )
            ecosystem = infer_ecosystem(update.name)
            if ecosystem is None:
                return MergeDecision(
                    "hold",
                    "osv_ecosystem_unknown",
                    f"Could not determine the OSV.dev ecosystem for {update.name}; treating as not verified.",
                )
            try:
                vulns = check_package_vulnerabilities(ecosystem, update.name, update.new_version)
            except OsvCheckUnavailable as e:
                return MergeDecision(
                    "hold",
                    "osv_check_unavailable",
                    f"Could not verify {update.name}@{update.new_version} against OSV.dev ({e}); "
                    "treating as not verified.",
                )
            if vulns:
                ids = ", ".join(v.id for v in vulns)
                return MergeDecision(
                    "hold",
                    "osv_vulnerability_found",
                    f"{update.name}@{update.new_version} has known vulnerabilities per OSV.dev: {ids}.",
                    needs_human_review=True,
                )

    return MergeDecision(
        "merge",
        "eligible",
        f"{bump_type} bump of {package}: not excluded, no hold label, all checks green, PR old enough.",
    )
