from src.agents.merge_policy import evaluate, is_excluded, parse_bump_title, semver_bump_type
from src.config import DependabotPolicy
from tests.test_github_tools import make_check_run, make_pr


def policy(**overrides):
    return DependabotPolicy(**{"min_pr_age_minutes": 0, **overrides})


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
    pr = make_pr(check_runs=[make_check_run("unit-tests", "success")])
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


def test_evaluate_holds_excluded_package_even_if_patch():
    pr = make_pr(check_runs=[make_check_run("unit-tests", "success")])
    pr.title = "chore(deps): bump github.com/sigstore/cosign/v3 from 3.1.2 to 3.1.3 (#3)"
    decision = evaluate(pr, policy(excluded_packages=["github.com/sigstore/cosign"]))
    assert decision.decision == "hold"
    assert decision.rule == "excluded_package"


def test_evaluate_holds_on_hold_label():
    pr = make_pr(labels=["hold"])
    pr.title = "chore(deps): bump foo from 1.0.0 to 1.0.1 (#4)"
    decision = evaluate(pr, policy())
    assert decision.decision == "hold"
    assert decision.rule == "hold_label"


def test_evaluate_holds_when_ci_not_green():
    pr = make_pr(check_runs=[])
    pr.title = "chore(deps): bump foo from 1.0.0 to 1.0.1 (#5)"
    decision = evaluate(pr, policy())
    assert decision.decision == "hold"
    assert decision.rule == "ci_not_green"


def test_evaluate_holds_when_pr_too_new():
    pr = make_pr(created_minutes_ago=0)
    pr.title = "chore(deps): bump foo from 1.0.0 to 1.0.1 (#6)"
    decision = evaluate(pr, policy(min_pr_age_minutes=30))
    assert decision.decision == "hold"
    assert decision.rule == "too_new"


def test_evaluate_holds_unparseable_title():
    pr = make_pr()
    pr.title = "Update dependencies"
    decision = evaluate(pr, policy())
    assert decision.decision == "hold"
    assert decision.rule == "unparseable_bump"


def test_evaluate_respects_patch_only_policy():
    pr = make_pr(check_runs=[make_check_run("unit-tests", "success")])
    pr.title = "chore(deps): bump foo from 1.0.0 to 1.1.0 (#7)"  # minor
    decision = evaluate(pr, policy(auto_merge="patch_only"))
    assert decision.decision == "hold"
    assert decision.rule == "policy_patch_only"
