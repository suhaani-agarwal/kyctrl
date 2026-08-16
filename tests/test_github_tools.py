from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.tools.github_tools import (
    _STATUS_MARKER,
    _upsert_comment,
    pr_age_minutes,
    pr_checks_all_green,
    pr_files,
    pr_labels,
    set_repo_variable,
)


def make_status(context, state):
    return SimpleNamespace(context=context, state=state)


def _combined_state(statuses: list) -> str:
    """Mirrors GitHub's own combined-status rollup rule (empty/no report ==
    'pending', i.e. not green — never treated as an implicit pass)."""
    if not statuses:
        return "pending"
    if any(s.state in ("failure", "error") for s in statuses):
        return "failure"
    if all(s.state == "success" for s in statuses):
        return "success"
    return "pending"


def make_pr(*, created_minutes_ago=0, labels=(), statuses=(), files=()):
    pr = MagicMock()
    pr.created_at = datetime.now(timezone.utc) - timedelta(minutes=created_minutes_ago)
    pr.get_labels.return_value = [SimpleNamespace(name=name) for name in labels]
    statuses = list(statuses)
    commit = MagicMock()
    commit.get_combined_status.return_value = SimpleNamespace(
        state=_combined_state(statuses), statuses=statuses, total_count=len(statuses)
    )
    pr.base.repo.get_commit.return_value = commit
    pr.get_files.return_value = [
        SimpleNamespace(filename=f["filename"], status=f["status"], additions=1, deletions=0, patch="")
        for f in files
    ]
    return pr


def test_pr_age_minutes():
    pr = make_pr(created_minutes_ago=42)
    assert 41 <= pr_age_minutes(pr) <= 43


def test_pr_labels():
    pr = make_pr(labels=["hold", "dependencies"])
    assert pr_labels(pr) == {"hold", "dependencies"}


def test_checks_all_green_true_when_all_success():
    pr = make_pr(statuses=[make_status("unit-tests", "success"), make_status("lint", "success")])
    assert pr_checks_all_green(pr) is True


def test_checks_all_green_false_when_any_failed():
    pr = make_pr(statuses=[make_status("unit-tests", "success"), make_status("lint", "failure")])
    assert pr_checks_all_green(pr) is False


def test_checks_all_green_false_when_no_checks_reported():
    pr = make_pr(statuses=[])
    assert pr_checks_all_green(pr) is False


def test_checks_all_green_only_looks_at_required_checks():
    pr = make_pr(statuses=[make_status("unit-tests", "success"), make_status("flaky-e2e", "failure")])
    assert pr_checks_all_green(pr, required_checks=["unit-tests"]) is True


def test_pr_files_shape():
    pr = make_pr(files=[{"filename": "go.mod", "status": "modified"}])
    files = pr_files(pr)
    assert files[0]["filename"] == "go.mod"
    assert files[0]["status"] == "modified"


def test_set_repo_variable_edits_existing():
    gh = MagicMock()
    repo = gh.get_repo.return_value
    var = repo.get_variable.return_value
    set_repo_variable(gh, "suhaani-agarwal/kyctrl-demo-target", "AI_MAINTAINER_ENABLED", "false")
    var.edit.assert_called_once_with("false")


def test_set_repo_variable_creates_when_missing():
    gh = MagicMock()
    repo = gh.get_repo.return_value
    repo.get_variable.side_effect = Exception("404")
    set_repo_variable(gh, "suhaani-agarwal/kyctrl-demo-target", "AI_MAINTAINER_ENABLED", "true")
    repo.create_variable.assert_called_once_with("AI_MAINTAINER_ENABLED", "true")


def test_upsert_comment_creates_when_no_existing_status_comment():
    get_comments = MagicMock(return_value=[SimpleNamespace(body="unrelated human comment")])
    create_comment = MagicMock()

    _upsert_comment(get_comments, create_comment, "held: too new")

    create_comment.assert_called_once()
    (posted_body,) = create_comment.call_args.args
    assert "held: too new" in posted_body
    assert _STATUS_MARKER in posted_body


def test_upsert_comment_edits_existing_status_comment_instead_of_reposting():
    """This is the fix for the demo repo spamming ~6 near-identical 'held'
    comments on one PR across repeated test runs — a bot re-run should
    update its own status comment, not stack a new one on top."""
    existing = SimpleNamespace(body=f"held: too new\n\n{_STATUS_MARKER}", edit=MagicMock())
    other = SimpleNamespace(body="a real contributor's comment", edit=MagicMock())
    get_comments = MagicMock(return_value=[other, existing])
    create_comment = MagicMock()

    _upsert_comment(get_comments, create_comment, "held: checks_unavailable")

    create_comment.assert_not_called()
    other.edit.assert_not_called()
    existing.edit.assert_called_once()
    (new_body,) = existing.edit.call_args.args
    assert "checks_unavailable" in new_body
    assert _STATUS_MARKER in new_body
