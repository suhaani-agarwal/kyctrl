from src.agents.issue_fsm import current_state, state_label, validate_transition


def test_current_state_none_when_no_ai_label():
    assert current_state({"bug", "triage"}) is None


def test_current_state_detects_ai_label():
    assert current_state({"bug", "triage", state_label("needs-repro-info")}) == "needs-repro-info"


def test_valid_first_transition_from_none():
    result = validate_transition({"bug", "triage"}, "needs-repro-info")
    assert result.ok is True
    assert result.from_state is None
    assert result.to_state == "needs-repro-info"


def test_invalid_skip_ahead_transition():
    result = validate_transition({"bug", "triage"}, "repro-requested")
    assert result.ok is False
    assert "not a valid transition" in result.reason


def test_invalid_unknown_state():
    result = validate_transition(set(), "closed-wontfix")
    assert result.ok is False
    assert "not a known state" in result.reason


def test_terminal_state_has_no_outgoing_transitions():
    labels = {"bug", state_label("ready-for-human")}
    result = validate_transition(labels, "needs-repro-info")
    assert result.ok is False


def test_needs_repro_info_to_repro_requested_is_valid():
    labels = {"bug", state_label("needs-repro-info")}
    result = validate_transition(labels, "repro-requested")
    assert result.ok is True
