from src.audit import AuditWriter, entry_to_dict, get_engine


def make_writer(tmp_path):
    engine = get_engine(str(tmp_path / "test-audit.sqlite3"))
    return AuditWriter(engine)


def test_write_and_recent(tmp_path):
    writer = make_writer(tmp_path)
    entry = writer.write(
        trigger_event="pull_request.opened",
        external_id="gh-pr-1",
        workflow_name="dependabot_auto_merge",
        agent_decision="merged",
        decision_reason="patch bump, CI green, not excluded",
        agent_reasoning_summary="Safe to merge.",
        action_taken="approve_and_merge_pr",
        action_result="success",
        can_be_reverted=True,
        revert_command="git revert <sha>",
        total_cost_usd=0.014,
    )
    assert entry.id is not None
    recent = writer.recent()
    assert len(recent) == 1
    assert recent[0].agent_decision == "merged"


def test_stats_aggregates_by_workflow_and_cost(tmp_path):
    writer = make_writer(tmp_path)
    for i in range(3):
        writer.write(
            trigger_event="pull_request.opened",
            external_id=f"gh-pr-{i}",
            workflow_name="dependabot_auto_merge",
            agent_decision="merged",
            action_taken="approve_and_merge_pr",
            action_result="success",
            total_cost_usd=0.01,
        )
    writer.write(
        trigger_event="issues.opened",
        external_id="gh-issue-1",
        workflow_name="issue_triage",
        agent_decision="labeled:kind/bug",
        action_taken="add_label,comment_on_issue",
        action_result="success",
        total_cost_usd=0.02,
    )
    stats = writer.stats()
    assert stats["total_actions"] == 4
    assert stats["by_workflow"]["dependabot_auto_merge"] == 3
    assert stats["by_workflow"]["issue_triage"] == 1
    assert stats["total_cost_usd"] == 0.05


def test_mark_overridden_sets_fields(tmp_path):
    writer = make_writer(tmp_path)
    entry = writer.write(
        trigger_event="pull_request.opened",
        external_id="gh-pr-1",
        workflow_name="dependabot_auto_merge",
        agent_decision="merged",
        action_taken="approve_and_merge_pr",
        action_result="success",
    )
    writer.mark_overridden(entry.id, by="jimbugwadia")
    updated = writer.recent()[0]
    assert updated.overridden_by == "jimbugwadia"
    assert updated.overridden_at is not None
    assert writer.stats()["overridden_count"] == 1


def test_entry_to_dict_is_json_serializable(tmp_path):
    writer = make_writer(tmp_path)
    entry = writer.write(
        trigger_event="issues.opened",
        external_id="gh-issue-1",
        workflow_name="issue_triage",
        agent_decision="labeled:kind/bug",
        action_taken="add_label",
        action_result="success",
    )
    import json

    json.dumps(entry_to_dict(entry))  # raises if not serializable
