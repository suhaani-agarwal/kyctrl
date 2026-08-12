# Kyverno AI Maintainer Assistant
## Project Requirements, Aims & Tool Decisions
### LFX Mentorship — Sep–Nov 2026

---

## 1. Problem Statement (Jim's Words, Interpreted Precisely)

Kyverno's maintainers burn significant time on **low-judgment, repetitive tasks** that compete directly with code review, design, and roadmap work:

- Reviewing and merging ~10–15 Dependabot PRs per week — each requires CI check, semver check, and a click
- Keeping contributor PRs rebased against `main` when they go stale — doing this manually signals poor maintainer responsiveness
- Triaging incoming issues — classifying bug vs. feature vs. question, applying labels, asking for missing repro info — happening at a volume that creates >48h first-response times
- Reproducing bugs — maintainer has to spin up a cluster, apply the policy, verify the behavior themselves
- Running the wrong CI scope — running the 90-minute full suite when a PR only touched `pkg/cel/` wastes runner time and slows feedback
- Answering repeat questions in Slack/GitHub Discussions — "does Kyverno support X?" answered for the hundredth time

The consequence: contributors experience slow feedback, PRs go stale, issues pile up. The project loses contributors before they become regulars.

---

## 2. What Jim Is Actually Asking For (Read Carefully)

Jim used very specific language. Every word matters:

**"Sandboxed"** — the agent runs in isolation. It cannot touch things it wasn't given permission to touch. No surprises.

**"Permission-scoped"** — least-privilege GitHub App. It has exactly the permissions required for each action and nothing more. It cannot push to `main`. It cannot modify `api/kyverno/v1`. It cannot delete branches.

**"Autonomous"** — it decides and acts without a human approving each individual action. This is the hard part. Not "draft and a human approves." It actually merges. It actually comments. It actually applies labels.

**"Auditable"** — every decision is logged. You can look back at any action and see: what triggered it, what the agent reasoned, what it did, and what the result was. A maintainer must be able to audit a full week of bot activity in 5 minutes.

**"Revertible"** — all actions can be undone. A merged Dependabot PR can be reverted. A label can be removed. A comment can be deleted. The bot never makes irreversible changes.

**"Always via comments, labels, draft PRs — not unreviewed direct pushes"** — this is the hard constraint. The bot can approve and merge (through GitHub's merge API, which respects branch protection). It cannot `git push` directly to `main`.

**"Config-driven"** — a single `.github/ai-maintainer.yaml` file controls everything. A maintainer can change behavior without touching code.

**"Kill switch"** — one label or one repo variable flip stops everything instantly.

---

## 3. Phased Deliverables (Jim's Exact Phases, Interpreted)

### Phase 0: Repository Intelligence Layer
**What Jim wants:** Before any automation, make the repo machine-readable.

Concrete deliverables:
- Expand `AGENTS.md` at repo root with a machine-readable task index (JSON/YAML manifest of all `make` targets with descriptions)
- Add per-directory `AGENTS.md` stubs in: `pkg/engine/`, `pkg/webhooks/`, `pkg/controllers/`, `test/conformance/`
- Each stub documents: what the package does, key entry points, which files are codegen'd (don't touch), which tests cover it
- A `path-to-suite map`: a YAML file that maps source paths to the conformance/unit test suites that cover them. E.g., `pkg/engine/handlers/validation/` → `test/conformance/chainsaw/validate/`
- A "safe automation boundaries" doc: explicit list of what the agent may modify autonomously vs. what requires human review

**Why this comes first:** Phases 1–4 all consume this metadata. The diff-to-test mapper (Phase 2) literally uses the path-to-suite map. The agent in Phase 1 uses the safe boundaries doc to decide what it can touch.

---

### Phase 1: Sandbox + GitHub App + Core Automation
**What Jim wants:** A working, deployed, config-driven agent that handles the highest-volume repetitive tasks safely.

Four sub-deliverables:

**1a. Dependabot Auto-Merge**
- Triggers on: Dependabot/Renovate PR opened or synchronized
- Reads config: `dependabot.auto_merge` policy from `.github/ai-maintainer.yaml`
- Checks: is it patch or minor? (configurable). Is CI green (all required checks)? Does it have a `hold` label? Is the package in the exclusion list (e.g., `cosign`, `notary`)?
- If all pass: approve the PR via GitHub API + merge via squash
- If major or CI fails: post a structured comment with a summary and flag `needs-human-review`
- Logs every decision to the audit trail

**1b. PR Hygiene — Branch Update + Stale Nudge**
- Branch update: after a push to `main`, find PRs that are behind, rebases/merges them automatically, re-triggers CI
- Stale nudge: after N idle days (configurable), post a contextual nudge comment to the author and reviewers
- After M days past the nudge with no activity: escalate with a second comment, optionally apply `lifecycle/stale` label
- Respects: `do-not-close` and `long-term` labels (never nudge these)

**1c. Codegen/Verify Gate**
- Triggers on: PRs touching `api/` directory
- Automatically runs `make codegen-all-code && make verify-codegen` in a sandbox
- Posts result as a PR check comment: pass (nothing to do) or fail (here's the diff, here's how to fix it)

**1d. Doc-Change Detection**
- On PRs that change behavior (not docs-only): checks if a corresponding doc change is present
- If missing: drafts a PR to `kyverno/website` with a stub doc change and links it in a comment on the original PR

---

### Phase 2: Diff-to-Test-Scope Mapper
**What Jim wants:** Smart CI — run only the tests relevant to a given diff, not the full 90-minute suite.

- Given a PR diff (list of changed files), map each changed path to its test suites using the Phase 0 path-to-suite YAML
- Output: a scoped test command (e.g., `make test-unit PKG=./pkg/engine/...` + `chainsaw test test/conformance/chainsaw/validate/`)
- Post the scoped test command as a comment on the PR
- Optionally: trigger the scoped test run directly via a workflow dispatch

---

### Phase 3: Issue Triage + Automated Reproduction
**What Jim wants:** First-response on new issues handled autonomously, including actually reproducing bugs.

Three sub-deliverables:

**3a. Classification + Labeling**
- Classifies incoming issues: `bug`, `feature`, `question`, `cli`, `webhook`, `docs`, `security`
- Applies the correct GitHub labels
- For `question` type: searches past issues + `kyverno.io` docs, drafts an answer, posts it

**3b. Missing Info Request**
- For `bug` reports: checks if the report contains Kyverno version, Kubernetes version, policy YAML, resource YAML, and actual vs expected behavior
- For each missing field: posts a structured comment requesting it (using a configurable template from `.github/ai-maintainer.yaml`)

**3c. Automated Reproduction (the impressive part)**
- For complete `bug` reports: spins up an ephemeral KinD cluster in a GitHub Actions runner
- Applies Kyverno via Helm (using the version from the issue)
- Applies the user's policy YAML and resource YAML from the issue body
- Captures the actual behavior (admission response, policy report, events)
- Compares against expected behavior stated in the issue
- Posts a structured comment: "I reproduced this on Kyverno v1.18.0 / Kubernetes v1.31. Actual: [output]. Expected: [from issue]. Confirmed bug." or "I could not reproduce this — here's what I got."

---

### Phase 4 (Stretch): Slack/Discussions Q&A Assistant
- Watches `#kyverno` Slack channel and GitHub Discussions for questions
- Searches `kyverno.io` docs and past resolved issues
- Answers when confidence is high; escalates to a human when low
- Always cites sources with links

---

## 4. Hard Constraints (Non-Negotiable)

These are Jim's guardrails. Every single one must be implemented:

| Constraint | How You Implement It |
|---|---|
| Least-privilege GitHub App | Separate GitHub App with fine-grained permissions per workflow. Never org-wide. |
| No direct push to main/release | Branch protection rules enforce this at GitHub level. App credentials cannot bypass it. |
| All merges require green CI | Agent checks GitHub Checks API before any merge action. |
| Config-driven behavior | `.github/ai-maintainer.yaml` is the single source of truth for all policies |
| Kill switch | Repo variable `AI_MAINTAINER_ENABLED=false` → agent reads this at the start of every run and aborts |
| Per-workflow kill switch | Individual workflows can be disabled in config without stopping others |
| Audit log | Every action logged: timestamp, trigger, agent decision, reasoning summary, action taken, result |
| Rate limiting | Max N actions per hour per workflow (configurable). Prevents runaway behavior. |
| Human override | Any bot-applied label can be manually removed to override the bot's decision |

---

## 5. Tool & Framework Decisions

Every decision below is justified against Jim's actual requirements and the current market.

---

### 5.1 The Agent Brain: Claude Agent SDK

**What it is:** Anthropic's official Python/TypeScript library that exposes the same agent loop powering Claude Code. <cite index="86-1">It gives you the same agent loop, tools, and context management that power Claude Code, packaged as a library you can embed in your own applications.</cite>

**Why this and not alternatives:**

- Jim explicitly listed "Claude Code" in his proposal. The Claude Agent SDK IS Claude Code as a library. Using it is direct alignment with his stated direction.
- <cite index="174-1">Anthropic uses the SDK internally for GitHub issue triage and Slack automation workflows</cite> — literally the same use case. This is documented publicly.
- Built-in: tool use loop, human-in-the-loop checkpoints, subagents, MCP client, persistent sessions. You don't build these yourself.
- <cite index="86-1">Out of the box you get file editing tools, bash execution, web search, a tool-use loop with optional human-in-the-loop checkpoints, subagents, persistent sessions, and first-class MCP client support.</cite>

**Why not LangGraph:** LangGraph requires you to define the graph manually — every node, every edge, every conditional. This adds 2–3x the code for no benefit when the Claude Agent SDK handles the reasoning loop automatically. LangGraph is better when you need model-agnosticism. You don't — Jim wants Claude.

**Why not OpenHands SDK:** Purpose-built for code tasks (bash, file edit, web browse). Not designed for GitHub workflow automation with webhook-driven triggers and policy config files.

**Why not CrewAI:** Role-based multi-agent abstraction. You have one agent. CrewAI's abstractions would hide what's happening — the opposite of auditable.

**Why not raw Anthropic API:** You'd write the tool loop yourself. Unnecessary when the Claude Agent SDK does it. Only reason to use raw API: non-Claude models.

**Install:** `pip install claude-agent-sdk`

---

### 5.2 GitHub Integration: PyGithub + GitHub App

**What it is:** PyGithub is the standard Python client for the GitHub API. A GitHub App is the right authentication model (not a PAT).

**Why GitHub App over PAT:**
- Fine-grained permissions: `pull_requests: write`, `issues: write`, `checks: read`, `contents: write` (only on non-protected refs)
- Installation-scoped: the App only has access to the repos it's installed on, not the entire org
- Audit trail: GitHub logs every App action with the App's identity
- Branch protection: the App physically cannot push to `main` even if it tried, because branch protection rules apply to App tokens too

**Exact permissions needed (minimum viable):**

| Permission | Level | Why |
|---|---|---|
| `pull_requests` | write | Approve, merge, comment on PRs |
| `issues` | write | Apply labels, post comments, close issues |
| `checks` | read | Verify CI status before merging |
| `contents` | write (non-protected refs only) | Rebase branches, update PR branches |
| `actions` | write | Trigger workflow dispatch for scoped test runs |
| `metadata` | read | Required for all Apps |

**Install:** `pip install PyGithub`

---

### 5.3 Webhook Receiver: FastAPI

**What it is:** Python async web framework.

**Why FastAPI:**
- Async-native: handles concurrent webhook events without blocking
- Proper HMAC-SHA256 signature verification (required for GitHub webhooks)
- Background tasks: return 200 immediately to GitHub, process the event async. GitHub requires a <10s response or it marks the delivery as failed.
- Auto-generated OpenAPI docs — useful for the demo

**Why not Flask:** Synchronous by default. A slow agent run would block the webhook receiver. Would need threading workarounds.

**Webhook events to subscribe to:**
- `pull_request` (opened, synchronize, closed, reopened)
- `pull_request_review` (submitted)
- `issues` (opened, edited, labeled)
- `issue_comment` (created)
- `push` (to `main` — triggers branch-update check)
- `schedule` (via GitHub Actions cron — triggers stale check)

---

### 5.4 Config System: PyYAML + Pydantic

**What it is:** YAML config file parsed with PyYAML, validated with Pydantic models.

**Why Pydantic:** Type-safe config. If someone puts a string where a boolean is expected in `.github/ai-maintainer.yaml`, the agent fails fast with a clear error instead of silently misbehaving.

**The config file (`.github/ai-maintainer.yaml`) governs:**
- Kill switch: `enabled: true/false`
- Per-workflow enable/disable
- Dependabot policy: `auto_merge: patch_and_minor | patch_only | none`
- Excluded packages list
- Stale nudge timers: `idle_days_before_nudge: 14`
- Issue triage templates
- Rate limits
- Safe automation boundaries (which paths the agent may touch)

---

### 5.5 Audit Log: SQLite via SQLAlchemy

**What it is:** Append-only structured log of every agent action stored in a SQLite database.

**Why SQLite:** Zero external dependency. Self-contained file. Can be committed to the repo for transparency (the audit log IS the audit trail). Queryable with SQL. Can be exported to JSON for the dashboard.

**Why not a logging service:** Adds an external dependency. Jim's requirement is that every action is "logged and traceable." A local SQLite file satisfies this and is inspectable by anyone with access to the runner.

**Schema (every row represents one agent action):**
```
id | timestamp | trigger_event | trigger_payload_ref | workflow_name | 
agent_decision | decision_reason | agent_reasoning_summary | 
action_taken | action_result | can_be_reverted | revert_command
```

---

### 5.6 Issue Reproduction Sandbox: KinD via GitHub Actions

**What it is:** KinD (Kubernetes in Docker) spun up inside a GitHub Actions runner to reproduce bug reports.

**Why KinD:** It's what Kyverno's own CI uses. Jim's issue specifically says "spin up a KinD cluster." Zero external infrastructure required — runs entirely inside a GitHub Actions runner.

**Why GitHub Actions (not a separate server):** The reproduction bot runs as a GitHub Actions workflow triggered by a webhook. The runner provides the Docker daemon needed for KinD. Free, no infra to maintain, inherits GitHub's security model.

**The reproduction flow:**
1. Agent parses issue body — extracts Kyverno version, policy YAML, resource YAML
2. Triggers a GitHub Actions workflow via `workflow_dispatch` with these as inputs
3. Workflow: creates KinD cluster → installs Kyverno via Helm at the specified version → applies policy YAML → applies resource YAML → captures admission response + policy reports + events
4. Workflow posts result as a comment on the issue via the GitHub API

**Tool:** `helm/kind-action@v1` (official GitHub Action for KinD)

---

### 5.7 Dashboard: React + Tailwind (single HTML file)

**What it is:** A simple real-time activity dashboard showing recent agent decisions.

**Why build this:** The demo video needs something visual. Raw logs in a terminal don't convey "auditable" to Jim the way a clean UI does. Showing "here's every decision the agent made this week, with the full reasoning expandable" is exactly what he asked for.

**What it shows:**
- Recent decisions: timestamp, trigger, decision, action taken
- Expandable "reasoning" panel per decision (the agent's full chain of thought)
- Kill switch toggle (sets the repo variable via GitHub API)
- Stats: PRs auto-merged this week, issues triaged, maintainer hours saved estimate

**Why not a full React app:** Overkill for a demo. A single HTML file with Tailwind CDN and vanilla JS calling a FastAPI endpoint that reads from SQLite is enough.

---

### 5.8 Deployment: Docker + GitHub Actions self-hosted (demo) or Railway (quick demo)

**For the prototype demo:** Railway.app (free tier). The FastAPI receiver runs there. Receives webhooks. Calls Claude Agent SDK. Posts back to GitHub. Takes 5 minutes to deploy.

**For the real project (what gets built during the mentorship):** The agent runs as a GitHub Actions workflow. This is the right long-term architecture because:
- No server to maintain
- Inherits GitHub's security model
- Runs in the same environment as Kyverno's CI
- Scales to zero (no cost when not running)
- The KinD reproduction sandbox only works inside GitHub Actions anyway

**Architecture in production:**
```
GitHub Event → GitHub Actions workflow trigger →
Runner: FastAPI receives webhook → Claude Agent SDK reasons →
PyGithub executes action → SQLite audit log updated →
Dashboard reads from log
```

---

## 6. What the Prototype Must Demo (The Video)

The prototype is not the full project. It's proof that you understand the architecture and can execute Phase 1.

**What to build before Aug 18:**
- Phase 0: The 4 AGENTS.md stubs (already described — this is a PR, not code)
- Phase 1a: Dependabot auto-merge (full working bot)
- Phase 1b: Stale nudge (full working bot)
- Phase 3a + 3b: Issue classification + missing-info request (full working bot)

**What the video must show (5 minutes):**

1. Open `.github/ai-maintainer.yaml` — show the config file and explain each section (30s)
2. Trigger a Dependabot-style PR in the demo repo — show the agent reasoning in a terminal stream, then show the PR getting approved and merged, then show the audit log entry (90s)
3. Kill switch — set `AI_MAINTAINER_ENABLED=false`, trigger another PR, show the agent stopping immediately with "disabled — no action taken" in the log (30s)
4. File a vague issue — show the agent classify it as `bug`, identify missing fields (no version, no repro YAML), and post a contextual comment requesting them (60s)
5. Show the dashboard — recent activity, expandable reasoning per decision, stats (30s)
6. "In the 12-week mentorship, I'll extend this to Phase 2 (diff-to-test mapper) and Phase 3c (automated KinD reproduction). The architecture is already designed for it." (30s)

---

## 7. What Makes This Different From Everything Else (The Proposal Angle)

This is what you write in your proposal to show you understand the market:

**Existing tools and their gaps:**
- Prow (what Kyverno uses now): deterministic, command-driven, no reasoning. A human types `/lgtm`. Your bot reasons about whether to lgtm automatically.
- CodeRabbit (what Kubernetes just rolled out): reviews code but never acts. Posts comments. A human still approves and merges. No issue triage, no Dependabot handling.
- PR-Agent: review on demand when a human triggers it. Not autonomous.
- withastro/triagebot-action: the closest thing — but Astro-specific, no policy config, not designed for CNCF reuse.
- TriageBot (SaaS): drafts responses for human approval. Not autonomous. SaaS dependency.

**The gap you fill:** A unified, policy-driven, auditable autonomous maintainer agent that is:
- CNCF-project-aware (understands DCO, CLA, Kyverno's codegen requirements, chainsaw conformance structure)
- Config-driven via a single YAML file (not hardcoded rules)
- Fully auditable with per-decision reasoning logs
- Reusable by other CNCF projects (the config schema and framework are project-agnostic)
- Built on Claude Agent SDK — directly aligned with Jim's stated direction

**The timing:** GitHub surveyed 500+ open source maintainers. 60% want help with issue triage. The Kubernetes community is actively experimenting with CodeRabbit right now. The CNCF is looking for what comes next. This project is positioned exactly at that inflection point.

---

## 8. Project Repo Structure

```
kyverno-ai-maintainer/
│
├── README.md                         # What it is, how to install, how to configure
├── .github/
│   ├── ai-maintainer.yaml            # Example config file
│   └── workflows/
│       ├── ai-maintainer.yaml        # GitHub Actions: main workflow (webhook → agent)
│       └── reproduce-issue.yaml      # GitHub Actions: KinD reproduction workflow
│
├── src/
│   ├── main.py                       # FastAPI app entry point + webhook receiver
│   ├── config.py                     # Pydantic models for ai-maintainer.yaml
│   ├── audit.py                      # SQLite audit log (SQLAlchemy)
│   │
│   ├── agents/                       # One file per workflow
│   │   ├── dependabot.py             # Claude Agent SDK: Dependabot auto-merge
│   │   ├── pr_hygiene.py             # Claude Agent SDK: branch update + stale nudge
│   │   ├── codegen_gate.py           # Claude Agent SDK: codegen verify
│   │   ├── issue_triage.py           # Claude Agent SDK: classify + label + missing-info
│   │   └── reproduction.py           # Triggers KinD reproduction workflow
│   │
│   ├── tools/                        # Tool definitions given to the agent
│   │   ├── github_tools.py           # All GitHub API calls (PyGithub wrappers)
│   │   └── kyverno_tools.py          # Kyverno-specific: parse policy YAML, check codegen
│   │
│   └── dashboard/
│       └── index.html                # Single-file dashboard (Tailwind CDN + vanilla JS)
│
├── tests/
│   ├── test_dependabot.py
│   ├── test_issue_triage.py
│   └── test_config.py
│
├── Dockerfile                        # Container for the FastAPI receiver
├── docker-compose.yml                # Local dev
└── requirements.txt
```

---

## 9. Timeline (12 Weeks, Sep 7 – Nov 27)

| Weeks | Focus | Deliverable |
|---|---|---|
| 1–2 | Phase 0 | 4 AGENTS.md stubs, path-to-suite YAML, safe-boundaries doc, task index |
| 3–4 | GitHub App + FastAPI skeleton | Webhook receiver live, config loading, audit log working |
| 5–6 | Phase 1a | Dependabot auto-merge agent deployed and tested |
| 7–8 | Phase 1b + 1c | PR hygiene agent + codegen gate |
| 9–10 | Phase 2 | Diff-to-test mapper |
| 11 | Phase 3a + 3b | Issue classification + missing-info request |
| 12 | Phase 3c + docs | KinD reproduction harness + final report + blog post |
| Stretch | Phase 4 | Slack/Discussions Q&A |

---

## 10. Key Links

| Resource | URL |
|---|---|
| Jim's issue | https://github.com/kyverno/kyverno/issues/16665 |
| LFX application | Search "Kyverno AI" on mentorship.lfx.linuxfoundation.org |
| Claude Agent SDK docs | https://platform.claude.com/docs/en/agent-sdk/overview |
| Claude Agent SDK Python | `pip install claude-agent-sdk` |
| PyGithub | https://github.com/PyGithub/PyGithub |
| Kyverno AGENTS.md | https://github.com/kyverno/kyverno/blob/main/AGENTS.md |
| Kyverno existing workflows | https://github.com/kyverno/kyverno/tree/main/.github/workflows |
| KinD GitHub Action | https://github.com/marketplace/actions/kind-cluster |
| withastro/triagebot-action (closest prior art) | https://github.com/withastro/triagebot-action |
| Kyverno website issues (for doc contributions) | https://github.com/kyverno/website/issues |
| CNCF Slack #kyverno | cloud-native.slack.com |