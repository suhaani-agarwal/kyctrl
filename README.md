# kyctrl — an AI Maintainer Assistant for Kyverno

A working prototype for [kyverno/kyverno#16665](https://github.com/kyverno/kyverno/issues/16665):
an autonomous, sandboxed, auditable agent that handles Kyverno's routine
maintainer work — reviewing Dependabot PRs, triaging incoming issues — so
human maintainers spend their time on code review, design, and roadmap
work instead.

This is not a review bot that posts suggestions for a human to act on. It
reasons about each situation using project-specific knowledge (a Kyverno
*skill pack*, not hardcoded rules), makes a decision grounded in a
config file any maintainer can edit, acts autonomously through comments,
labels, and PR merges, and logs its full reasoning for every action.

## What's actually running here

| Workflow | Status |
|---|---|
| Dependabot/Renovate auto-merge | ✅ working — deterministic policy engine + Claude Agent SDK |
| Issue triage (classify, missing-info, redirect) | ✅ working — label-FSM + Claude Agent SDK |
| Kill switch (config file + live repo variable) | ✅ working, interrupts mid-run via `can_use_tool` |
| Audit log + dashboard | ✅ working — SQLite, single-page UI |
| PR hygiene (stale nudge, branch update) | 🗺️ designed, not built (see `docs/planning/`) |
| Diff-to-test-scope mapper | 🗺️ designed, not built |
| Automated KinD bug reproduction | 🗺️ designed, not built |
| Slack/Discussions Q&A | 🗺️ stretch goal for the mentorship term |

See `docs/planning/kyctrl_requirements.md` for the full feature set and
`docs/planning/kyctrl_extra_features.md` for the longer-term vision
(memory, multi-agent specialization, proactive monitoring, self-improving
skills) this prototype is architecturally seeded to grow into.

## Why the merge/label decisions aren't "just ask the LLM"

Whether to merge a Dependabot PR, and which issue-triage state transitions
are legal, are **policy questions with deterministic answers** — so they're
answered by plain Python (`src/agents/merge_policy.py`,
`src/agents/issue_fsm.py`), never by the model. The Claude Agent SDK is
used for what's actually a judgment call: explaining a decision in plain
English, and recognizing genuinely ambiguous situations (a misfiled
question issue, an unparseable PR title). The model is also never given a
capability it shouldn't have — the merge tool doesn't exist in a given run
unless the rule engine already cleared the PR, and the kill-switch toggle
is never exposed to the model as a tool at all.

## Architecture

```
GitHub webhook → FastAPI (HMAC-verified) → Event → handler registry
                                                        │
                              ┌─────────────────────────┴─────────────────────────┐
                              ▼                                                   ▼
                    merge_policy.evaluate()                          issue_fields / issue_fsm
                    (deterministic: semver,                          (deterministic: field
                     CI status, exclusions,                           completeness, valid
                     hold label, PR age)                               label transitions)
                              │                                                   │
                              ▼                                                   ▼
                 Claude Agent SDK query()                          Claude Agent SDK query()
                 sandboxed, per-PR-scoped tools,                   sandboxed, per-issue-scoped
                 can_use_tool re-checks kill switch                tools, same kill-switch gate
                              │                                                   │
                              └─────────────────────────┬─────────────────────────┘
                                                         ▼
                                          SQLite audit log (every decision,
                                          reasoning, cost, revert command)
                                                         │
                                                         ▼
                                          Dashboard (/,  /api/audit, /api/stats)
```

## Quickstart (local dev)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY + either GITHUB_PAT (dev) or the App vars

python3 -m pytest tests/ -q          # 62 tests, no network required

uvicorn src.main:app --reload --port 8000
# in another terminal, forward a GitHub App's webhooks to localhost:
pip install pysmee && pysmee forward <smee-channel-url> http://127.0.0.1:8000/webhook
```

Open `http://127.0.0.1:8000/` for the dashboard.

## Configuration

Everything the agent does is governed by [`.github/ai-maintainer.yaml`](.github/ai-maintainer.yaml)
— per-workflow enable/disable, the Dependabot merge policy, issue-triage
label taxonomy and required fields, rate limits, and safe automation
boundaries. Two independent kill switches exist: `enabled: false` in that
file (reviewed, needs a PR), and the `AI_MAINTAINER_ENABLED` repo Actions
variable (instant, toggleable from the dashboard, always wins).

## Skill pack

`skills/kyverno/` is where all Kyverno-specific knowledge lives — as
markdown, not Python — so any maintainer can improve the agent's behavior
with a PR to a doc, no code required:

- `dependabot-policy.md` — grounded in Kyverno's real `.github/dependabot.yml`
- `issue-triage.md` — grounded in Kyverno's real issue templates and label taxonomy
- `safe-boundaries.md` — what the agent may touch autonomously vs. never
- `path-to-suite-map.yaml` — source path → test suite map (Phase 0 deliverable)

## Tests

```bash
python3 -m pytest tests/ -q
```

62 tests, all offline (mocked GitHub/SDK calls) — they verify the
deterministic rule engines, the audit log, config validation, and the
webhook receiver's dispatch/signature-verification logic without spending
API credits or touching a real repo.
