"""Label-driven finite state machine for issue lifecycle.

Per kyctrl_extra_features.md Dimension 6 and the withastro/triagebot
pattern cited in kyctrl_requirements.md: GitHub labels ARE the state
store. States live under an `ai/` prefix so they never collide with
Kyverno's own human-facing labels (`bug`, `enhancement`, `triage`, ...) —
this FSM tracks *the bot's* progress on an issue, not the issue's real
lifecycle, which stays entirely human-owned.

The FSM decides which transitions are valid. It never decides what to
*say* at a transition — that's the agent's system-prompt-guided judgment,
enforced by `agents/issue_triage.py`'s `transition_issue_state` tool
calling `validate_transition` before touching any label.
"""

from __future__ import annotations

from dataclasses import dataclass

LABEL_PREFIX = "ai/"

STATES = ["needs-repro-info", "repro-requested", "ready-for-human", "redirected"]

# None = "the bot hasn't touched this issue yet" (no ai/* label present).
TRANSITIONS: dict[str | None, set[str]] = {
    None: {"needs-repro-info", "ready-for-human", "redirected"},
    "needs-repro-info": {"repro-requested"},
    "repro-requested": {"ready-for-human"},
    "ready-for-human": set(),  # terminal — a human owns it from here
    "redirected": set(),  # terminal
}


def state_label(state: str) -> str:
    return f"{LABEL_PREFIX}{state}"


def current_state(labels: set[str]) -> str | None:
    for state in STATES:
        if state_label(state) in labels:
            return state
    return None


@dataclass
class TransitionResult:
    ok: bool
    from_state: str | None
    to_state: str
    reason: str


def validate_transition(labels: set[str], target: str) -> TransitionResult:
    current = current_state(labels)
    if target not in STATES:
        return TransitionResult(False, current, target, f"{target!r} is not a known state")
    if target not in TRANSITIONS.get(current, set()):
        return TransitionResult(False, current, target, f"{current!r} -> {target!r} is not a valid transition")
    return TransitionResult(True, current, target, "ok")
