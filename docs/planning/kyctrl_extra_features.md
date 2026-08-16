Dimension 1: From Reactive to Proactive

Right now we described an agent that reacts — a webhook fires, it responds. The bigger vision is an agent that hunts for problems before they're reported.

Predictive PR Risk Scoring. Before any human reviews a PR, the agent has already read the diff, cross-referenced it against historical bugs in the same files, checked whether similar changes caused regressions in the past, and posted a risk score. "This PR touches pkg/engine/cel/ — the last 3 regressions in this file were introduced by changes to variable resolution logic. Confidence this needs extra scrutiny: high." This is not review commentary. This is intelligence about where to spend human attention.

Anomaly Detection on Repository Health. The agent monitors metrics over time — issue open rate, PR merge velocity, CI pass rate, contributor return rate. When something deviates from baseline, it surfaces it proactively. "This week's issue open rate is 40% above the 90-day average. The spike started Tuesday and clusters around the ClusterPolicy API. No known release or blog post explains it." A maintainer didn't ask for this. The agent noticed it and told them.

Contributor Churn Early Warning. The agent tracks contributor engagement signals — when did they last comment, are their PRs going un-reviewed for longer than usual, did they open something and then go quiet. Before a contributor disappears, the agent flags them and suggests a maintainer reach out. This is the kind of thing that separates a thriving community from one that slowly hollows out.

Dimension 2: From Single Agent to Multi-Agent System

Right now it's one agent doing everything. The more interesting architecture is specialized subagents that collaborate:

The Triage Agent handles classification, labeling, missing info. It's lightweight and fires on every issue. Cheap to run.

The Reproduction Agent is expensive and only fires when Triage confirms a complete bug report. It has access to KinD, runs real clusters, has elevated capabilities. It reports back to Triage with findings.

The Security Agent runs separately from everything else, with no access to public comment posting. It reads vulnerability reports, cross-references CVE databases, runs dependency analysis, and produces a private report that goes only to maintainers — never to the public issue thread.

The Pattern Agent runs on a weekly schedule. It reads everything that happened — all issues, all PRs, all CI runs — and builds a structured understanding of patterns. "The three issues this week about ClusterPolicy and namespaceSelector are related. Root cause is probably a single regression in v1.18.2." It then files a single tracking issue linking all three.

The Coach Agent analyzes contributor PRs and produces targeted, encouraging feedback on code style, test coverage gaps, and Kyverno conventions. Not a code reviewer — a mentor. It knows the AGENTS.md deeply and teaches the contributor how to navigate the codebase.

This multi-agent architecture is exactly what Jim was gesturing at when he named OpenHands, OpenClaw, and Hermes — platforms where specialized agents collaborate rather than one monolithic agent doing everything.

Dimension 3: Memory and Learning

This is the biggest gap in what we described. Right now the agent has no memory between events. Every Dependabot PR is treated as if it's the first one ever. Every issue is classified from scratch.

The technical implementation is Graphiti (`pip install graphiti-core`) — the open-source temporal knowledge graph engine behind Zep, Apache 2.0, backed by a single Neo4j container (one more service in `docker-compose.yml`, not a new infra category). The choice matters architecturally, not just as a benchmark line: a vector-first memory store like Mem0 embeds facts and retrieves by similarity, which answers "what does this contributor usually do" reasonably well. It cannot answer "how has the relationship between ClusterPolicy issues and version releases evolved over the last three months," because there's no relationship to traverse — only nearby vectors. Graphiti models every fact as a typed node and a typed, timestamped edge — `Issue#1234 -[RELATED_TO {reason: "shared_label:ClusterPolicy"}]-> Issue#1289`, `Contributor#alice -[OPENED]-> PR#4521`, `PackageBump#lodash-4.17.20 -[CAUSED]-> Regression#...` — and every edge carries a bi-temporal range: when the fact became true in the world (`valid_from`/`valid_to`), tracked separately from when the agent learned it. "Did this contributor have a DCO problem on their first PR but not their second" is a graph traversal with a time filter, not a hope that the right memory is nearest in embedding space. That's the exact shape of reasoning a repository intelligence layer needs, and it's the shape LongMemEval — the benchmark built specifically to test temporal reasoning and multi-hop entity relationships — measures, where Graphiti outperforms Mem0 by roughly 15 points.

This stays generalized the same way the rest of this framework already is. The Graphiti client, the episode-write helper, and its tool namespace live in the **core engine**, so every agent gets the identical read-before-reasoning/write-after-acting contract regardless of which repository it's running against — the same split as the rule engine (`merge_policy.py`, `issue_fsm.py`) versus the skill docs. What's project-specific is the *ontology*: which entity and edge types exist (`Issue`, `Contributor`, `PackageBump`, `CAUSED_REGRESSION`, `RELATED_TO`). That ships as part of the skill pack, exactly like Kyverno's label taxonomy in `skills/kyverno/issue-triage.md` or its exclusion list in `skills/kyverno/dependabot-policy.md` — another CNCF project adopting the framework brings its own entity types the way it brings its own labels; the graph engine underneath doesn't change.

The wiring is uniform across every agent. On write: right where `dependabot.py` and `issue_triage.py` already call `audit.write(...)` at the end of a run, they add one `graphiti.add_episode(...)` describing what happened, and the returned node/edge UUIDs are what `audit.py`'s existing `memory_refs` column was already reserved for — a forward-compatible seam, not a new migration. On read: before reasoning, every agent calls `graphiti.search(...)` — Graphiti's built-in hybrid semantic + BM25 + graph-traversal search — and the relevant nodes and edges get appended alongside the skill doc as system-prompt context. Because Graphiti ships its own MCP server, this is exposed to the Claude Agent SDK as a tool with no glue code: one more entry in `mcp_servers` next to the existing `github` and `state` servers, and one more prefix (`mcp__memory__`) added to `can_use_tool`'s allow-list in `src/runtime.py` — so the kill switch and tool-scoping guarantees that already govern GitHub actions apply to memory reads and writes for free, with nothing new to enforce.

What each memory type becomes concretely:

Episodic Memory. "Last week it merged a lodash bump that later turned out to have a breaking change the CI didn't catch" becomes a `CAUSED_REGRESSION` edge from that PackageBump node to a Regression node, `valid_from` set to when the regression actually surfaced — not when the agent found out. Seeing another lodash bump this week, `graphiti.search("lodash bump risk")` surfaces that edge before any decision is made, and the agent applies extra scrutiny — checks the changelog explicitly before approving. It isn't recalling a vibe; it's traversing one specific edge with one specific reason attached.

Cross-Issue Pattern Memory. When three issues describe similar symptoms, the agent doesn't just log a similarity score — it writes explicit `RELATED_TO` edges between the Issue nodes with the reason recorded on the edge. A new issue matching the pattern surfaces the whole connected subgraph, so the response can cite "#4521, #4489, #4502" as issues the graph actually knows are linked, not just the nearest neighbor by embedding.

Contributor Profile Memory. A Contributor node accumulates timestamped edges to every PR, issue, and review it has touched. "How responsive are they to review feedback" and "did they forget DCO sign-off on their first PR but not their second" are both temporal traversals over that edge history — the second question specifically requires ordering facts in time and comparing them, which a pure similarity search over embeddings cannot do. A first-time contributor (no outgoing edges yet) gets the full welcome; a veteran gets a concise, direct response.

Repository Evolution Memory. A directory or package is itself a node. Issue and regression edges into it accumulate over time, and when the rate of new edges into a previously-quiet node spikes, that's a graph-native version of the same anomaly Dimension 1's proactive monitoring looks for — except here the alert comes with the connected subgraph explaining *why*, for free, instead of a metric crossing a threshold with no story attached.

Dimension 4: The Agent That Improves Itself

This is the most ambitious idea and the one that would genuinely impress Jim the most.

Skill Refinement from Feedback. Every time a maintainer overrides the agent — removes a label it applied, reverts a merge it made, edits a comment it posted — that's a signal. The agent reads these overrides and understands what it got wrong. Over time it proposes changes to its own skill files. "I've been misclassifying issues about ClusterPolicy generation as kind/bug when maintainers consistently relabel them as kind/feature. I'm proposing this update to the issue-triage skill." The skill improvement PR is reviewed by a human before being merged.

Confidence Calibration. The agent tracks its own decision confidence and its own error rate. When it notices it's less confident in a certain category — say, distinguishing CLI bugs from webhook bugs — it flags this and suggests that a maintainer add more examples to the skill file. The agent actively asks for the knowledge it's missing.

A/B Testing Its Own Policies. When a policy question is ambiguous — should stale PRs be nudged after 14 days or 21 days? — the agent can run a soft A/B test: try both on different PR cohorts, measure which produces better outcomes (PR gets updated and merged vs PR gets closed), and recommend a config change with evidence.

Dimension 5: From GitHub to the Full Maintainer Surface

Right now we described GitHub webhooks as the input. But maintainers live across multiple surfaces:

Slack Integration — Not Just Q&A. The agent isn't just a chatbot in Slack. It's an active participant. When a discussion in Slack converges on a decision — "okay we'll close these three issues as duplicates" — the agent recognizes the decision and executes it, linking the conversation to the actions it took. Slack becomes a natural language interface to the repository.

The Maintainer Briefing. Every Monday morning, every maintainer gets a personalized digest tailored to their specific areas of ownership. Not a generic weekly report — one that knows you're the person responsible for the CEL engine and surfaces only the issues, PRs, and CI anomalies relevant to that area.

Release Manager Mode. When a release branch is cut, the agent shifts into a different operating mode. It monitors the release branch for cherry-pick requests, validates backports, tracks which fixes are in the release vs main, drafts the release notes, monitors the release CI separately from main CI, and alerts on any branch divergence that might cause a broken release.

The Policy Observatory. The Kyverno website hosts hundreds of community-contributed sample policies. The agent continuously monitors this library — when a new Kubernetes version ships and changes an API, it identifies which sample policies might be affected, tests them against the new API, and files issues or PRs against the policy library automatically.

Dimension 6: Making It Deterministic Where It Matters

You're right that pure LLM reasoning is not enough for everything. The most impressive systems combine LLM reasoning for judgment calls with deterministic, rule-based systems for things that should never be wrong.

Finite State Machine for Issue Lifecycle. An issue moves through explicit states: needs-triage → triage-in-progress → needs-repro-info → repro-requested → repro-confirmed → assigned → in-progress → pr-opened → resolved. The agent manages state transitions explicitly using GitHub labels as the state store. This is exactly how withastro's triagebot works and it's brilliant — the FSM is auditable, reversible, and completely deterministic. The LLM handles the content of what's said at each transition. The FSM handles what transitions are valid.

Rule Engine for Merge Policy. Whether to merge a Dependabot PR is NOT a judgment call — it's a policy check. This should be a pure deterministic rule engine: semver check, CI check, exclusion list check, minimum age check, hold label check. The LLM explains the decision in natural language. The rule engine makes the actual binary yes/no. Never let the LLM decide something that has a deterministic answer.

Commit Graph Analysis. Pattern matching on the commit history to detect things like: files that always break together, directories that have high churn recently suggesting instability, contributors who consistently produce PRs that need multiple review rounds suggesting they'd benefit from more guidance. Pure graph algorithms, no LLM needed.

Dimension 7: Observatory and Observability

The agent should be observable from outside — not just an audit log, but a real-time window into the health of the repository.

Repository Health Dashboard. A public-facing dashboard (served at kyverno.io/health or similar) showing: issue response time trends, PR merge velocity, CI reliability over time, contributor growth/churn, Dependabot debt accumulation. These metrics are computed continuously by the agent from the repository data.

Agent Confidence Dashboard. A separate internal view showing where the agent is confident and where it isn't. Red cells where it's been overridden frequently. Green cells where its decisions have been consistently correct. This is how you know where the skill files need improvement.

Cost and Token Tracking. Every agent run logs its LLM cost. The dashboard shows cost per workflow per week. A maintainer can see that the reproduction agent costs $2 per run and decide whether the value justifies it. This is the kind of operational transparency that makes a production system trustworthy.

What This Becomes

Put all of these dimensions together and you don't have a bot. You have a living intelligence layer on top of a software repository. It perceives the full state of the repository across all surfaces — GitHub, Slack, CI, the issue tracker, the commit graph, the contributor graph. It maintains memory of everything that has ever happened. It acts proactively on what it notices, not just reactively to events. It improves itself from feedback. It delegates to specialized subagents for complex tasks. It publishes its own health and confidence so humans can understand it and trust it.

This is what "sandboxed coding agent in the style of OpenHands/Hermes" actually means at full scope. Not a webhook handler. A repository nervous system.

For the proposal: describe the full vision. Build the reactive webhook prototype to prove you can execute. But frame it explicitly as "Phase 1 of a system that will evolve to have memory, proactive monitoring, multi-agent coordination, and self-improvement." Jim will see immediately that you understand the destination, not just the first step.

The most impressive thing you can say in that proposal is: "The agent I'm building for this mentorship term doesn't just react to events. By the end of week 12, it will remember what it has done before, recognize patterns across issues, propose improvements to its own skill files, and produce weekly health reports that a maintainer couldn't produce manually. Here's how."