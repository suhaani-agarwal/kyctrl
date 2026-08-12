from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.tools.github_tools import pr_age_minutes, pr_checks_all_green, pr_files, pr_labels, set_repo_variable


def make_check_run(name, conclusion):
    return SimpleNamespace(name=name, conclusion=conclusion)


def make_pr(*, created_minutes_ago=0, labels=(), check_runs=(), files=()):
    pr = MagicMock()
    pr.created_at = datetime.now(timezone.utc) - timedelta(minutes=created_minutes_ago)
    pr.get_labels.return_value = [SimpleNamespace(name=name) for name in labels]
    commit = MagicMock()
    commit.get_check_runs.return_value = list(check_runs)
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
    pr = make_pr(check_runs=[make_check_run("unit-tests", "success"), make_check_run("lint", "success")])
    assert pr_checks_all_green(pr) is True


def test_checks_all_green_false_when_any_failed():
    pr = make_pr(check_runs=[make_check_run("unit-tests", "success"), make_check_run("lint", "failure")])
    assert pr_checks_all_green(pr) is False


def test_checks_all_green_false_when_no_checks_reported():
    pr = make_pr(check_runs=[])
    assert pr_checks_all_green(pr) is False


def test_checks_all_green_only_looks_at_required_checks():
    pr = make_pr(check_runs=[make_check_run("unit-tests", "success"), make_check_run("flaky-e2e", "failure")])
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
