from github import GithubException

from src.agents.merge_policy import (
    evaluate,
    group_bump_type,
    is_excluded,
    parse_bump_title,
    parse_commit_trailer,
    parse_pr_dependency_updates,
    resolve_bump,
    semver_bump_type,
)
from src.config import DependabotPolicy
from tests.test_github_tools import make_pr, make_status


def policy(**overrides):
    return DependabotPolicy(**{"min_pr_age_minutes": 0, **overrides})


# Real Dependabot commit-message shapes (trailer format per the fetch-metadata
# README / Dependabot's own docs) used across the trailer-parsing tests below.

SINGLE_PATCH_MSG = """Bump github.com/sigstore/cosign/v3 from 3.1.2 to 3.1.3

Bumps github.com/sigstore/cosign/v3 from 3.1.2 to 3.1.3.

---
updated-dependencies:
- dependency-name: github.com/sigstore/cosign/v3
  dependency-type: direct:production
  update-type: version-update:semver-patch
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

KUBERNETES_GROUP_MIXED_MSG = """Bump the kubernetes group with 3 updates

Bumps the kubernetes group with 3 updates: k8s.io/api, k8s.io/apimachinery and k8s.io/client-go.

Updates `k8s.io/api` from 0.31.0 to 0.31.1
Updates `k8s.io/apimachinery` from 0.31.0 to 0.31.1
Updates `k8s.io/client-go` from 0.31.0 to 0.32.0

---
updated-dependencies:
- dependency-name: k8s.io/api
  dependency-type: indirect
  dependency-group: kubernetes
  update-type: version-update:semver-patch
- dependency-name: k8s.io/apimachinery
  dependency-type: indirect
  dependency-group: kubernetes
  update-type: version-update:semver-patch
- dependency-name: k8s.io/client-go
  dependency-type: direct:production
  dependency-group: kubernetes
  update-type: version-update:semver-minor
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

KUBERNETES_GROUP_WITH_MAJOR_MSG = KUBERNETES_GROUP_MIXED_MSG.replace(
    "update-type: version-update:semver-minor", "update-type: version-update:semver-major"
)

OTEL_GROUP_ALL_PATCH_MSG = """Bump the otel group with 2 updates

Bumps the otel group with 2 updates: go.opentelemetry.io/otel and go.opentelemetry.io/otel/trace.

Updates `go.opentelemetry.io/otel` from 1.20.0 to 1.20.1
Updates `go.opentelemetry.io/otel/trace` from 1.20.0 to 1.20.1

---
updated-dependencies:
- dependency-name: go.opentelemetry.io/otel
  dependency-type: indirect
  dependency-group: otel
  update-type: version-update:semver-patch
- dependency-name: go.opentelemetry.io/otel/trace
  dependency-type: indirect
  dependency-group: otel
  update-type: version-update:semver-patch
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

MALFORMED_TRAILER_MSG = """Bump foo

---
updated-dependencies:
- dependency-name: foo
  update-type: [unterminated
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

RENOVATE_NO_TRAILER_MSG = "chore(deps): update dependency foo to v1.2.3"


def test_semver_bump_type_patch():
    assert semver_bump_type("3.1.2", "3.1.3") == "patch"


def test_semver_bump_type_minor():
    assert semver_bump_type("v0.31.0", "v0.32.0") == "minor"


def test_semver_bump_type_major():
    assert semver_bump_type("1.9.0", "2.0.0") == "major"


def test_semver_bump_type_unknown_on_unparsable():
    assert semver_bump_type("main", "latest") == "unknown"


def test_parse_bump_title_real_kyverno_dependabot_title():
    # Real merged PR title from kyverno/kyverno history.
    package, bump = parse_bump_title(
        "chore(deps): bump github.com/sigstore/cosign/v3 from 3.1.2 to 3.1.3 (#17008)"
    )
    assert package == "github.com/sigstore/cosign/v3"
    assert bump == "patch"


def test_parse_bump_title_group_bump_is_unknown():
    package, bump = parse_bump_title("chore(deps): bump the kubernetes group with 3 updates")
    assert package == "kubernetes group"
    assert bump == "unknown"


def test_is_excluded_matches_module_path_substring():
    excluded = ["github.com/sigstore/cosign", "k8s.io/client-go"]
    assert is_excluded("github.com/sigstore/cosign/v3", excluded) is True
    assert is_excluded("github.com/google/go-containerregistry", excluded) is False


def test_evaluate_merges_clean_patch_bump():
    pr = make_pr(statuses=[make_status("unit-tests", "success")])
    pr.title = "chore(deps): bump github.com/google/go-containerregistry from 1.0.0 to 1.0.1 (#1)"
    decision = evaluate(pr, policy())
    assert decision.decision == "merge"
    assert decision.rule == "eligible"


def test_evaluate_holds_major_bump():
    pr = make_pr()
    pr.title = "chore(deps): bump k8s.io/client-go from 0.31.0 to 1.0.0 (#2)"
    decision = evaluate(pr, policy())
    assert decision.decision == "hold"
    assert decision.rule == "major_bump"
    assert decision.needs_human_review is True


def test_evaluate_holds_excluded_package_even_if_patch():
    pr = make_pr(statuses=[make_status("unit-tests", "success")])
    pr.title = "chore(deps): bump github.com/sigstore/cosign/v3 from 3.1.2 to 3.1.3 (#3)"
    decision = evaluate(pr, policy(excluded_packages=["github.com/sigstore/cosign"]))
    assert decision.decision == "hold"
    assert decision.rule == "excluded_package"
    assert decision.needs_human_review is True


def test_evaluate_holds_on_hold_label():
    pr = make_pr(labels=["hold"])
    pr.title = "chore(deps): bump foo from 1.0.0 to 1.0.1 (#4)"
    decision = evaluate(pr, policy())
    assert decision.decision == "hold"
    assert decision.rule == "hold_label"
    assert decision.needs_human_review is False  # a human already acted (applied the label); no extra label needed


def test_evaluate_holds_when_ci_not_green():
    pr = make_pr(statuses=[])
    pr.title = "chore(deps): bump foo from 1.0.0 to 1.0.1 (#5)"
    decision = evaluate(pr, policy())
    assert decision.decision == "hold"
    assert decision.rule == "ci_not_green"
    assert decision.needs_human_review is False  # will clear on its own once CI finishes


def test_evaluate_holds_when_pr_too_new():
    pr = make_pr(created_minutes_ago=0)
    pr.title = "chore(deps): bump foo from 1.0.0 to 1.0.1 (#6)"
    decision = evaluate(pr, policy(min_pr_age_minutes=30))
    assert decision.decision == "hold"
    assert decision.rule == "too_new"
    assert decision.needs_human_review is False  # will clear on its own once the PR ages past the minimum


def test_evaluate_holds_unparseable_title():
    pr = make_pr()
    pr.title = "Update dependencies"
    decision = evaluate(pr, policy())
    assert decision.decision == "hold"
    assert decision.rule == "unparseable_bump"
    assert decision.needs_human_review is True


def test_evaluate_holds_when_checks_unavailable_instead_of_crashing():
    """Regression test: this used to be an unguarded status-API call inside
    evaluate() — a 403 (missing token permission, or historically the
    Checks API being fundamentally unreachable by any fine-grained PAT —
    see the comment on pr_checks_all_green) propagated as an uncaught
    GithubException and crashed the whole agent run instead of holding."""
    pr = make_pr()
    pr.title = "chore(deps): bump foo from 1.0.0 to 1.0.1 (#8)"
    pr.base.repo.get_commit.return_value.get_combined_status.side_effect = GithubException(
        403, {"message": "Resource not accessible by personal access token"}, None
    )
    decision = evaluate(pr, policy())
    assert decision.decision == "hold"
    assert decision.rule == "checks_unavailable"
    assert decision.needs_human_review is False  # a token/permissions issue, not a judgment call on the PR


def test_evaluate_respects_patch_only_policy():
    pr = make_pr(statuses=[make_status("unit-tests", "success")])
    pr.title = "chore(deps): bump foo from 1.0.0 to 1.1.0 (#7)"  # minor
    decision = evaluate(pr, policy(auto_merge="patch_only"))
    assert decision.decision == "hold"
    assert decision.rule == "policy_patch_only"


# --- Commit-trailer parsing: the fix for grouped PRs always being "unknown" ---


def test_parse_commit_trailer_single_package():
    updates = parse_commit_trailer(SINGLE_PATCH_MSG)
    assert len(updates) == 1
    assert updates[0].name == "github.com/sigstore/cosign/v3"
    assert updates[0].dependency_type == "direct:production"
    assert updates[0].bump_type == "patch"
    assert updates[0].new_version == "3.1.3"


def test_parse_commit_trailer_grouped_kubernetes():
    updates = parse_commit_trailer(KUBERNETES_GROUP_MIXED_MSG)
    by_name = {u.name: u for u in updates}
    assert len(updates) == 3
    assert by_name["k8s.io/api"].bump_type == "patch"
    assert by_name["k8s.io/client-go"].bump_type == "minor"
    assert by_name["k8s.io/client-go"].dependency_type == "direct:production"
    assert by_name["k8s.io/client-go"].new_version == "0.32.0"


def test_parse_commit_trailer_returns_empty_when_absent():
    assert parse_commit_trailer(RENOVATE_NO_TRAILER_MSG) == []


def test_parse_commit_trailer_returns_empty_on_malformed_yaml():
    assert parse_commit_trailer(MALFORMED_TRAILER_MSG) == []


def test_group_bump_type_returns_highest():
    assert group_bump_type(parse_commit_trailer(OTEL_GROUP_ALL_PATCH_MSG)) == "patch"
    assert group_bump_type(parse_commit_trailer(KUBERNETES_GROUP_MIXED_MSG)) == "minor"
    assert group_bump_type(parse_commit_trailer(KUBERNETES_GROUP_WITH_MAJOR_MSG)) == "major"


def test_group_bump_type_unknown_wins_over_known_siblings():
    updates = parse_commit_trailer(OTEL_GROUP_ALL_PATCH_MSG)
    updates[0].bump_type = "unknown"  # simulate one unrecognized update-type in the group
    assert group_bump_type(updates) == "unknown"


def test_group_bump_type_empty_is_unknown():
    assert group_bump_type([]) == "unknown"


def test_parse_pr_dependency_updates_reads_all_commits_deduped_last_wins():
    pr = make_pr(commit_messages=[SINGLE_PATCH_MSG, SINGLE_PATCH_MSG])
    updates = parse_pr_dependency_updates(pr)
    assert len(updates) == 1  # de-duped by name, not doubled


def test_resolve_bump_prefers_trailer_over_title():
    pr = make_pr(commit_messages=[OTEL_GROUP_ALL_PATCH_MSG])
    pr.title = "Update dependencies"  # would be "unknown" under title parsing alone
    package, bump_type = resolve_bump(pr)
    assert bump_type == "patch"
    assert "go.opentelemetry.io/otel" in package


def test_resolve_bump_falls_back_to_title_when_no_trailer():
    pr = make_pr(commit_messages=[RENOVATE_NO_TRAILER_MSG])
    pr.title = "chore(deps): bump foo from 1.0.0 to 1.0.1 (#9)"
    package, bump_type = resolve_bump(pr)
    assert package == "foo"
    assert bump_type == "patch"


# --- evaluate() using the trailer as its primary source ---


def test_evaluate_merges_grouped_patch_bump_via_trailer():
    """Regression test for the confirmed bug: every module in an all-patch
    grouped PR used to fall through to bump_type="unknown" (title parsing
    can't see per-package data) and hold as "unparseable_bump" no matter
    how trivial the bump. Kyverno groups most of its real Dependabot
    traffic (kubernetes/sigstore/otel), so this was the common case, not an
    edge case."""
    pr = make_pr(statuses=[make_status("unit-tests", "success")], commit_messages=[OTEL_GROUP_ALL_PATCH_MSG])
    pr.title = "Bump the otel group with 2 updates"
    decision = evaluate(pr, policy())
    assert decision.decision == "merge"
    assert decision.rule == "eligible"


def test_evaluate_holds_grouped_bump_as_major_when_any_member_is_major():
    pr = make_pr(statuses=[make_status("unit-tests", "success")], commit_messages=[KUBERNETES_GROUP_WITH_MAJOR_MSG])
    pr.title = "Bump the kubernetes group with 3 updates"
    decision = evaluate(pr, policy())
    assert decision.decision == "hold"
    assert decision.rule == "major_bump"
    assert decision.needs_human_review is True


def test_evaluate_excludes_group_member_by_name_even_if_others_pass():
    pr = make_pr(statuses=[make_status("unit-tests", "success")], commit_messages=[KUBERNETES_GROUP_MIXED_MSG])
    pr.title = "Bump the kubernetes group with 3 updates"
    decision = evaluate(pr, policy(excluded_packages=["k8s.io/client-go"], auto_merge="patch_and_minor"))
    assert decision.decision == "hold"
    assert decision.rule == "excluded_package"
    assert "k8s.io/client-go" in decision.reason
    assert decision.needs_human_review is True
