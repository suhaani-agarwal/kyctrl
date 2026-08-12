# Kyverno AI Maintainer Assistant
## Vision, Problem Statement, Features & Tech Stack

---

## The Problem We're Solving

Kyverno is a CNCF graduated project used in production by Amazon, Coinbase, Bloomberg, Vodafone, LinkedIn, and Spotify. It has a small team of core maintainers and a fast-growing contributor community. As the project has scaled, a painful and growing mismatch has emerged: the louder the community becomes, the more maintainer hours get consumed by work that does not require their expertise.

Specifically, Jim has identified these recurring drains on maintainer time:

Reviewing and merging 10–15 Dependabot PRs every week — each requires a semver check, a CI status check, and a click to merge. Keeping contributor PRs rebased against main — when a PR goes stale because nobody rebased it, the contributor experience degrades and people stop contributing. Triaging a high volume of incoming issues — classifying bug vs. feature vs. question, applying labels, asking for missing reproduction information — with first-response times stretching to 48 hours or more. Reproducing bug reports — a maintainer spins up a Kubernetes cluster, installs Kyverno at the reported version, applies the policy, observes the behavior, and writes it all up. Running the wrong CI scope — running the full 90-minute conformance suite for a PR that only touched one package. Answering the same questions in Slack and GitHub Discussions for the hundredth time.

The consequence Jim described precisely: this work competes directly with code review, design, and roadmap work. Contributors experience slow feedback. PRs go stale. New contributors open a pull request, hear nothing for a week, and never return.

---

## What We Are Building

An **AI Maintainer Assistant** — a fully autonomous, sandboxed, auditable agent that handles the mechanical layer of open source maintainership so that human maintainers can focus exclusively on decisions that require human judgment.

This is not a review bot that posts suggestions. It is not a chatbot that drafts replies for humans to approve. It is not a static automation script with hardcoded rules. It is an agent that **reasons** about each situation using project-specific knowledge, makes a decision grounded in configurable policy, **acts** autonomously, and logs its complete chain of thought for every single action it takes.

Every aspect of the agent's behavior is governed by a single configuration file that lives inside the target repository. Any maintainer can adjust its behavior, restrict its scope, or disable it completely — instantly — without touching code.

**The key architectural vision:** This is built as a generalizable framework, not a Kyverno-specific tool. The core agent engine is project-agnostic. Kyverno-specific intelligence — how to classify Kyverno issues, how to reproduce Kyverno bugs, what Kyverno's label taxonomy means, which source paths map to which test suites, what the codegen gate requires — is packaged as a **skill pack** that the agent loads when working on the Kyverno repository. Any other CNCF project can adopt the entire framework by contributing their own skill pack. The deliverable is not a bot for Kyverno. It is a maintainer AI platform for the CNCF ecosystem, with Kyverno as the first reference implementation.

This lives in a standalone repository inside the Kyverno GitHub organization. The target repository contains only a configuration file and structured metadata files. The agent reads configuration from the target repository remotely at runtime — exactly how Prow works for Kubernetes and how withastro's triagebot works for Astro.

---

## What Exists Today and Why It Is Not Enough

The market has split into two camps that do not overlap.

On one side are review-only tools. CodeRabbit rolled out across several Kubernetes projects in mid-2026. It reads diffs and posts inline suggestions. It never acts. PR-Agent does the same on demand when a human types a slash command. GitHub Copilot code review requires a per-person paid license. None of these tools apply a label, merge a PR, triage an issue, request missing information, or reproduce a bug. They offload zero maintainer hours.

On the other side are general-purpose coding agents. OpenHands resolves GitHub issues by cloning the repository and writing code. It is designed for fixing bugs — not for maintainer workflow automation. It has no concept of Dependabot merge policies, label taxonomies, DCO sign-off requirements, or codegen gates.

The closest prior art is withastro's triagebot-action, which does automated bug reproduction for the Astro framework. But it is Astro-specific, has no configurable policy system, has no Dependabot handling, no PR hygiene, and is not designed for reuse across projects.

Prow — which Kyverno already uses in a lite form — is the original maintainer bot for the Kubernetes ecosystem. It executes commands that humans type. It has no reasoning capability. It cannot decide whether a Dependabot PR is safe to merge. It cannot classify an issue. It cannot write a contextual response.

The gap: a unified, reasoning, policy-driven autonomous agent that is CNCF-project-aware, config-driven, fully auditable at the decision level, and extensible via a skill system to any project in the ecosystem.

---

## The Skill System — The Core Architectural Idea

A skill pack is a collection of structured markdown documents that give the agent project-specific knowledge and reasoning instructions. Each document is human-readable and editable — any maintainer can improve the agent's behavior for their project by submitting a pull request to the skill files. No Python knowledge required.

The Kyverno skill pack includes: how to classify Kyverno issues and what each label means, how to reproduce a Kyverno bug using KinD and Helm, what the codegen gate requires and when to run it, which source paths map to which conformance test suites, and which paths the agent must never modify autonomously. When the agent receives an event from the Kyverno repository, it loads the relevant skill document as grounding context before reasoning about what to do.

Another CNCF project contributes its own skill pack with its own label taxonomy, its own reproduction steps, its own safe boundaries. Same core engine. Different skills. Different project. This is how the framework generalizes across the ecosystem.

---

## Full Feature Set

### Phase 0 — Repository Intelligence Layer

Jim described this explicitly as the prerequisite for everything else. Before any automation can work intelligently, the repository must be structured so that an agent — and a new human contributor — can navigate it without reading every file.

**Monorepo module boundary evaluation.** Kyverno is a single repo that depends on separate SDK and API packages. Jim asked specifically that this be evaluated — does restructuring module boundaries make the codebase more maintainable and more navigable for both agents and humans? This phase produces a concrete proposal and, if approved, the minimal changes to land it.

**Expanded AGENTS.md at the repository root.** The file already exists but is thin. It gets expanded with a machine-readable task index — a JSON or YAML manifest of every build, test, and lint target available via make — so that an agent or a new contributor does not have to parse the Makefile to understand what commands are available and what they do.

**Per-directory AGENTS.md stubs in high-traffic areas.** New files in `pkg/engine/`, `pkg/webhooks/`, `pkg/controllers/`, and `test/conformance/` documenting what the package does, the key entry points, which files are generated and must never be manually edited, and which tests cover this package.

**Safe automation boundaries document.** An explicit structured document declaring which paths an agent may modify autonomously — dependency bumps, generated CRDs — and which paths always require human review — `api/kyverno/v1`, `pkg/cosign/`, `pkg/notary/`. This document is what gives the Phase 1 agent its guardrails.

**Path-to-test-suite map.** A structured YAML file mapping every significant source path in the repository to the unit and conformance test suites that cover it. This is the foundation that Phase 2's diff-to-test mapper reads at runtime.

**Structured PR labels and templates.** Standardize the PR label taxonomy and contribution templates so that an agent can reliably classify the scope of any change — docs-only, generated-code-only, breaking API change — without heuristics.

---

### Phase 1 — Core Automation Workflows

These are Jim's explicitly listed Phase 1 deliverables. All of them are in scope.

**Dependency PR handling — Dependabot and Renovate auto-merge.** When Dependabot or Renovate opens a pull request, the agent reads the configured merge policy — which semver bump types are eligible, which packages are excluded for security sensitivity, what the minimum PR age is. It checks whether all required CI checks have passed via the GitHub Checks API. It checks for a human-applied hold label. If everything satisfies the policy, it approves the PR and merges it via squash, posts a structured comment with its decision and reasoning, and writes a full audit entry. If anything fails the policy, it posts a summary explaining what it found and what decision a human needs to make, and applies a needs-human-review label. Major version bumps always go to humans with a structured summary of what changed.

**PR hygiene — branch update and stale PR nudging.** Two sub-workflows. The branch updater watches for pushes to main and automatically rebases or merges main into open contributor PRs that have fallen behind, then re-triggers CI. The stale nudge workflow tracks idle time — after a configurable number of days with no activity, it posts a contextual nudge to the author and assigned reviewers; after a further idle period, it escalates. Both workflows permanently respect exclusion labels like do-not-close, long-term, and on-hold.

**Scoped test selection.** Analyzes the diff of a pull request — the specific packages and directories changed — and uses the path-to-test-suite map from Phase 0 to compute the minimal set of unit and conformance tests that are relevant. Posts the exact scoped test command as a comment and can optionally trigger that scoped run directly via a GitHub Actions workflow dispatch event. Transforms the CI feedback loop from a 90-minute full-suite run into a targeted run of exactly what matters for the change.

**Issue triage — classification, labeling, missing info, and automated reproduction.** This is a four-part pipeline. First, the agent classifies every new issue as bug, feature, question, CLI, webhook, documentation, or security and applies the correct labels. Second, for questions it searches past resolved issues and kyverno.io documentation and drafts a contextual answer. Third, for bug reports it checks whether the minimum required information is present — Kyverno version, Kubernetes version, policy YAML, resource YAML, actual behavior, expected behavior — and posts a structured comment requesting any missing fields using a configurable template from the skill pack. Fourth — the most technically impressive piece — for complete bug reports it triggers a GitHub Actions workflow that spins up an ephemeral KinD cluster, installs Kyverno at the reported version via Helm, applies the policy and resource YAMLs extracted from the issue body, captures the actual admission response, policy reports, and Kubernetes events, and posts a structured comment on the issue: either confirming reproduction with full output or reporting that it could not reproduce with exact observations. This cuts the time from issue filed to confirmed bug reproduction from days to minutes.

**Slack and GitHub Discussions Q&A assistant.** Watches the #kyverno channel in CNCF Slack and GitHub Discussions for incoming questions. Searches indexed kyverno.io documentation and past resolved issues. When confidence exceeds a configured threshold, posts an answer with cited sources. When confidence is below threshold, escalates by tagging a maintainer. Never guesses and never answers without citations.

**Codegen and verify gate.** When a pull request touches the `api/` directory — which contains Kyverno's Kubernetes CRD type definitions — the agent automatically triggers `make codegen-all-code && make verify-codegen` and posts the result as a PR comment. If codegen is out of sync, it posts the exact diff and step-by-step fix instructions. If clean, it posts a pass confirmation and removes any pending codegen label.

**Doc change detection and draft PR generation.** When a pull request changes behavior but does not include a corresponding documentation update, the agent identifies the gap, drafts a stub pull request to the Kyverno website repository pre-filled with the relevant context, and posts a link in a comment on the original PR. The maintainer reviews and refines the draft rather than writing it from scratch.

---

### Phase 2 — Diff-to-Test Scope Mapper

Reads the pull request diff, uses the Phase 0 path-to-test-suite map to determine exactly which unit test packages and chainsaw conformance suites are relevant to the change, and outputs the precise scoped test command. Can trigger the scoped run directly via workflow dispatch. Eliminates the waste of a 90-minute full suite run for changes that touch one package.

---

### Phase 3 — Issue Triage and Automated Reproduction Harness

The full automated reproduction pipeline from Phase 1 is delivered as a first-class standalone harness with structured error handling, partial-result reporting, and a clear output schema. The harness is extensible — new reproduction scenarios for new Kyverno feature areas can be added to the skill pack without touching the harness code.

---

### Phase 4 — Slack and Discussions Q&A (Stretch Goal)

The Q&A assistant from Phase 1 is promoted to a full standalone workflow with a documentation index that is automatically kept current as kyverno.io updates, a configurable confidence threshold with per-topic tuning, and a feedback mechanism where maintainers can mark agent answers as correct or incorrect to improve future responses over time.

---

### Additional Capabilities Jim Listed Explicitly for Future Phases

Jim brainstormed all of these in the issue. Including them in the proposal with design thinking behind each signals that the candidate has read the issue thoroughly and can think at the roadmap level.

**Auto-draft release notes and changelog entries.** When a release is being prepared, the agent aggregates all merged PRs since the last tag, groups them by their labels — bug fix, feature, breaking change, docs-only, generated-code-only — and drafts structured release notes in the exact format Kyverno uses. The maintainer edits rather than writes from scratch. Eliminates one of the most time-consuming pre-release tasks.

**Flaky test detection.** Tracks CI results over time across all workflow runs. When a test fails intermittently — passing and failing with no code changes between runs — the agent flags it, opens a GitHub issue with the failure pattern, affected test name, log excerpts showing the flaky runs, and optionally applies a flaky label to the relevant conformance suite. Maintainers get data-driven signal instead of relying on anecdote.

**Auto-backport to release branches.** When a PR is merged with a backport label targeting a specific release branch, the agent automatically opens a corresponding backport PR targeting that branch. The backport PR is created but not merged — it requires a maintainer approval. This preserves human control over release branch contents while eliminating the mechanical work of creating the backport.

**License, CLA, and DCO sign-off checker.** Every Kyverno commit must carry a Signed-off-by trailer as required by the DCO. When a PR contains commits missing this trailer, the agent posts clear, step-by-step instructions including the exact git rebase --signoff command so the contributor can fix it immediately without hunting through documentation.

**First-time contributor welcome.** When a pull request is opened by someone with no prior merged contribution to the repository, the agent posts a personalized welcome message, links to the contributing guide and development setup, and points to the AGENTS.md for the specific package they touched. Reduces the friction that causes new contributors to churn before their first PR is merged.

**Security advisory triage.** When a vulnerability report is filed using the VULN-TEMPLATE.md, the agent cross-references the affected component against Kyverno's dependency graph, checks if the CVE is already tracked in a known advisory, and drafts an initial severity assessment for maintainer review. The security team receives a structured starting point rather than a raw unanalyzed report.

**Auto-suggest reviewers.** When a pull request is opened, the agent analyzes git history to find who has made the most recent and most significant changes to the files being modified and suggests them as reviewers via a comment. Based on actual code ownership history, not random assignment.

**Weekly maintainer digest.** A scheduled workflow that produces a structured weekly summary — open PRs ranked by age, issue backlog by category and age, CI flakiness trends, Dependabot PRs waiting for attention, approved PRs not yet merged — and posts it to a designated Slack channel. Gives maintainers a weekly snapshot without requiring them to manually triage the entire backlog.

**Policy YAML lint and dry-run bot.** When a PR contributes a new sample policy to charts/kyverno-policies or the Kyverno website policy library, the agent runs the Kyverno CLI against it in a KinD cluster and posts the lint result as a PR comment. Catches policy syntax errors, logical issues, and invalid field references before they are merged into the library that thousands of users reference daily.

---

## Hard Constraints — Required, Not Optional

Jim listed these verbatim as guardrails. Every one is non-negotiable and must be demonstrable in the prototype.

The agent runs in a sandboxed container with least-privilege credentials. It can write to issues and pull requests. It can never push directly to main or any release branch — branch protection rules at the GitHub level make this physically impossible regardless of what the agent attempts.

Every merge requires CI to pass. The agent queries the GitHub Checks API and confirms all required checks show success before any merge action. No exceptions.

Every automated action is logged with full reasoning. The audit log captures the trigger event, the agent's chain of thought, the decision made, the action taken, and the result. A maintainer must be able to review a full week of bot activity in under five minutes.

All behavior is controlled by `.github/ai-maintainer.yaml`. Read remotely at the start of every agent run. Maintainers change policies without touching code.

Kill switch. Setting the repository variable AI_MAINTAINER_ENABLED=false stops all agent activity instantly. Individual workflows can also be independently disabled in the config without affecting others.

Rate limiting. A configurable maximum number of actions per hour prevents runaway behavior in all edge cases.

Human override. Any label or action the bot applies can be manually reversed by a maintainer, and the agent respects those overrides in subsequent runs.

---

## Tech Stack and Tool Decisions

**Agent Brain: Claude Agent SDK.** Jim explicitly listed Claude Code in the Technologies field of the official LFX listing. The Claude Agent SDK is Claude Code packaged as a programmable Python library. Anthropic built it specifically for GitHub issue triage and Slack automation workflows — documented publicly and exactly this use case. Provides out of the box: tool use reasoning loop, human-in-the-loop checkpoints, subagent delegation (used in the reproduce pipeline), MCP client support, and persistent sessions. The skill markdown files map directly to the agent's system prompt context. Nothing about the reasoning loop needs to be built manually.

**GitHub Integration: GitHub App with Fine-Grained Permissions via PyGithub.** A GitHub App — not a Personal Access Token. Apps have installation-scoped credentials, every action is logged under the App's identity in GitHub's audit trail, branch protection rules apply identically to App tokens preventing any push to main, and tokens auto-rotate eliminating credential management overhead.

**Webhook Receiver: FastAPI.** Async-native Python framework. Returns 200 to GitHub within 10 seconds via background tasks while the agent processes asynchronously. Clean HMAC-SHA256 signature verification.

**Configuration and Validation: Pydantic and PyYAML.** Type-safe validation of the YAML configuration file. Misconfigured files produce clear human-readable errors, not silent misbehavior. Config is fetched from GitHub API at runtime — always fresh, never stale.

**Audit Log: SQLite via SQLAlchemy.** Zero external dependency. Append-only structured database. Every row is an immutable record of one agent action with timestamp, trigger, workflow, decision, reasoning summary, action taken, result, and revert instructions. Queryable, exportable, optionally committable to the repository for full transparency.

**Issue Reproduction Sandbox: KinD via GitHub Actions.** KinD is what Kyverno's own CI uses. Jim explicitly described spinning up a KinD cluster in the issue. Runs entirely inside a GitHub Actions runner — zero external infrastructure, zero cost when idle, inherits GitHub's security model.

**Skill System: Structured Markdown with YAML Frontmatter.** Inspired directly by withastro's triagebot-action which pioneered the skill directory pattern. Maintainers improve agent behavior by submitting PRs to markdown files — no Python knowledge required. Any CNCF project adopts the framework by contributing their own skill pack directory.

**Dashboard: Single-File HTML.** Self-contained HTML with Tailwind CDN and vanilla JavaScript, reading from a FastAPI endpoint backed by SQLite. Shows recent decisions with expandable per-decision reasoning, a live kill switch toggle, and weekly stats. No build step, no Node.js dependency.

**Deployment for prototype: Railway.app.** For the mentorship deliverable: GitHub Actions workflows triggered by webhook events — no always-on server, runs inside GitHub's security boundary, zero cost when idle.

---

## Where We Start

The first thing to build is a working prototype scoped to two complete end-to-end workflows — Dependabot auto-merge and issue classification with missing-info requests — real enough to demonstrate convincingly in a five-minute video before the application deadline.

The FastAPI receiver handles incoming webhook events from a demo repository. When a Dependabot PR arrives, the agent reads the configuration file, loads the Kyverno skill context, checks CI status and policy, and either merges the PR or flags it — streaming its reasoning visibly in the terminal, then logging the full decision and reasoning to SQLite. When a new issue is filed, the agent reads the issue triage skill, classifies the issue type, identifies missing fields from the report, and posts a contextual comment using the configurable template. The kill switch is wired and demonstrable — flipping the config stops the agent cold on the next event. The dashboard shows the audit log in real time with expandable reasoning per entry.

This is what gets built before August 18. The proposal describes the full 12-week arc. The video proves that Phase 1 is already real and working.