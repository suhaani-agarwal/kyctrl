Multi-agent architecture (Dimension 2) + Slack/Discussions Q&A bot

 Context

 kyctrl today runs two working agents (dependabot.py, issue_triage.py),
 each wired through a single seam: Event → EVENT_HANDLERS[event.type] →
 one handler → one AuditEntry. The codebase was deliberately seeded for
 more than this — audit.py already carries unused parent_run_id (commented
 "subagent hierarchies (Dimension 2)") and memory_refs columns, and
 events.py's Event.source docstring already anticipates "slack" /
 "cron" as future values. docs/kyctrl_extra_features.md Dimension 2 names
 five specialized subagents (Triage, Reproduction, Security, Pattern, Coach)
 that collaborate instead of one monolith doing everything, and the project's
 own README lists "Slack/Discussions Q&A" as the stretch goal for the
 mentorship term, directly answering what Jim asked for in
 kyverno/kyverno#16665:
 "answer common questions using project docs and link relevant issues/PRs,
 escalating to a human when confidence is low."

 This is the biggest infrastructure step this project has taken: it adds a
 Neo4j instance (new, local via docker-compose) as a shared graph store
 for two systems built in this pass — a LightRAG-indexed documentation
 graph (kyverno.io + resolved Q&A issues, Neo4j-backed, incrementally
 updatable, version-tagged) and a Slack Bolt app using Slack's native AI
 Assistant surface — plus real-but-simpler first versions of Coach, Security,
 Pattern, and Reproduction agents on the existing multi-agent foundation. A
 tree-sitter code-structure graph (for Coach/Pattern) and Graphiti
 temporal memory (Dimension 3 — agent history/override learning) are
 explicitly deferred to a fast-follow pass on the same Neo4j instance, once
 this Q&A path is proven — see "Deferred, not this pass" at the end.

 Every new agent still follows the codebase's existing rule: routing/
 handoff decisions between agents are deterministic Python, never an LLM
 router — consistent with Dimension 6 and with how merge_policy.py/
 issue_fsm.py already work. The Q&A bot's core safety property — it never
 answers without a real citation — is enforced the same way: a tool-call
 guardrail in plain Python, not a prompt instruction the model could ignore.

 ---
 1. Shared multi-agent foundation

 src/events.py — EVENT_HANDLERS becomes dict[str, list[AgentHandler]]
 so one event type can fan out to multiple independent agents (a
 pull_request triggers both dependabot.py and the new coach.py; an
 issues event triggers both issue_triage.py and the new
 security_agent.py). register_handler appends instead of overwriting. Add
 async def dispatch(event: Event) -> None that runs every registered
 handler for event.type concurrently via
 asyncio.gather(..., return_exceptions=True), logging (not raising) any
 handler's exception so one agent's failure never blocks its siblings.

 src/main.py — webhook endpoint's background_tasks.add_task(handler, event)
 becomes background_tasks.add_task(dispatch, event). Add discussion and
 discussion_comment to EXTERNAL_ID_FIELD. Mount the Slack Bolt adapter and
 the cron ingress (§3, §6). Import new agent modules for their
 @register_handler side-effects, same as the existing two imports.

 tests/test_main.py — test_webhook_accepts_valid_signature_and_dispatches
 currently does monkeypatch.setitem(EVENT_HANDLERS, "pull_request", fake_handler)
 (single value); update to the list form, and add a new test proving two
 handlers registered on the same event type both run, and a third proving one
 handler's exception doesn't stop the other's AuditEntry from being written.

 src/audit.py — no schema change (the columns already exist). Every new
 agent's handle_* gains an optional parent_run_id: int | None = None param
 threaded straight into audit.write(...), populated whenever one agent's
 deterministic decision hands off to another.

 src/runtime.py — extend _ALLOWED_TOOL_PREFIXES with the new
 mcp_servers dict keys the new agents use: mcp__qa__, mcp__pattern__,
 mcp__coach__, mcp__security__. Same enforcement pattern as today.

 Deterministic handoff rules:

 - issue_triage.py: after writing its own AuditEntry, if
 classification == "bug" and not missing and "security" not in labels,
 call reproduction.trigger_reproduction(issue_number, external_id, parent_run_id=entry.id).
 - security_agent.py registers independently on "issues", gated internally
 on "security" in labels. .github/ai-maintainer.yaml's
 issue_triage.exclusion_labels gains security so Triage skips these
 issues entirely — config-driven separation, no code coupling.
 - coach.py registers independently on "pull_request", gated on
 pr.user.login not in config.dependabot.bot_usernames (small shared
 helper, src/agents/_shared.py::is_bot_author, used by both
 dependabot.py and coach.py).
 - pattern_agent.py is triggered by new source="cron" Events (§6).

 ---
 2. Neo4j infrastructure (new)

 docker-compose.yml gains a neo4j service (official neo4j:5-community
 image): exposes 7687 (Bolt) and 7474 (browser/admin UI), NEO4J_AUTH
 from env, a named volume (neo4j_data) for persistence, and enables the
 APOC plugin (NEO4JLABS_PLUGINS: '["apoc"]') since neo4j-graphrag-python's
 schema/index setup and GraphRAG's import step both lean on APOC procedures.
 The kyctrl service gains depends_on: [neo4j].

 New env vars (.env.example): NEO4J_URI (bolt://neo4j:7687 in
 Docker, bolt://localhost:7687 for local dev outside Docker), NEO4J_USER,
 NEO4J_PASSWORD.

 src/graph.py (new, mirrors runtime.py's singleton style) —
 @lru_cache def get_neo4j_driver() -> neo4j.Driver wrapping
 neo4j.GraphDatabase.driver(...), used by everything below.

 New dependencies (requirements.txt): neo4j (official driver, shared
 by src/graph.py and LightRAG's Neo4j storage backend), lightrag-hku
 (indexing + hybrid retrieval, configured with graph_storage="Neo4JStorage"
 so it writes into the same Neo4j instance), markdownify (HTML → clean
 markdown for the docs crawler). crawl4ai (Playwright + Chromium) is
 deliberately not a base dependency — kyverno.io is server-rendered
 static HTML, so scripts/crawl_docs.py defaults to plain httpx +
 markdownify, no browser needed. Crawl4AI is documented as an optional,
 on-demand install (pip install crawl4ai + playwright install chromium)
 for the rare page that turns out to need JS rendering, invoked only via an
 explicit --js-render flag — so the default image stays free of the
 Playwright/Chromium cost. This is still the one part of this plan where
 "zero external dependency" (the stated philosophy behind SQLite/audit log
 elsewhere in this codebase) is a deliberate, acknowledged trade-off, just a
 much smaller one than originally scoped.

 ---
 3. Q&A Assistant — Slack + GitHub Discussions (the flagship piece)

 Doc graph pipeline (offline, run to build/refresh the index; incremental)

 1. scripts/crawl_docs.py — httpx + markdownify fetch kyverno.io
 (sitemap-seeded) and render clean markdown, no browser needed (the
 --js-render fallback shells out to the optional Crawl4AI install only
 for pages flagged as needing it). Also pulls closed/answered
 question-labeled issues via the GitHub search API (PyGithub — already a
 dependency). Every document gets: source_url (load-bearing for
 citations — the whole point of this pipeline), and a best-effort
 kyverno_version tag — parsed from the doc's URL path for versioned
 kyverno.io pages, or from the reporter-stated Kyverno version for
 issue-sourced docs, defaulting to "unversioned" when neither applies
 (conceptual/overview pages).
 2. scripts/build_doc_index.py — one step, not three: for each document,
 calls LightRAG.ainsert(text, ids=[source_url], file_paths=[source_url])
 with kyverno_version attached as chunk metadata, against a LightRAG
 instance configured with graph_storage="Neo4JStorage" pointed at the
 same Neo4j instance as src/graph.py. Because LightRAG dedups/updates by
 content hash, re-running this against a refreshed crawl only touches
 changed documents — this is what makes the cron refresh job (§6) cheap:
 no full rebuild, no re-embedding unchanged pages.

 This pipeline is run manually or via the new cron endpoint (§6,
 doc-index-refresh) — it's what keeps answers grounded in real, current,
 version-tagged content instead of the model's general knowledge.

 Retrieval

 src/tools/doc_retriever.py wraps rag.aquery(query, param=QueryParam(mode="hybrid"))
 (LightRAG's own hybrid vector+graph retrieval — no separate retriever
 library needed), exposing search_docs(query: str, top_k: int, target_version: str | None) -> list[Chunk]
 where each Chunk carries text, source_url, and kyverno_version. When
 target_version is given (the agent infers it from the question, or falls
 back to config.qa_assistant.default_kyverno_version), a deterministic
 post-retrieval step re-ranks/filters LightRAG's results to prefer chunks
 tagged with that version — falling back to the unfiltered ranking if nothing
 matches, so a version-specific question never silently comes back empty.
 This filtering step lives in plain Python outside LightRAG, same reasoning
 as the citation guardrail: a correctness property this important shouldn't
 depend on a library's query-time filtering support. This is the retrieval
 layer the Q&A agent's search_docs tool calls — the model chooses what to
 search for (and, ideally, what version the question is about); ranking is a
 fixed algorithm, not an LLM judgment call.

 Core agent

 src/agents/qa_assistant.py — answer_question(question_text, source, thread_ref, parent_run_id=None) -> AuditEntry:

 - Tool server exposes search_docs (above) and a structured
 propose_answer({answer, citations: list[str], confidence: "high"|"medium"|"low"})
 tool. propose_answer tracks every source_url actually returned by a
 prior search_docs call in this run (closure set) and rejects the tool
 call (is_error) if citations is empty or cites a URL never returned by
 search — this is what makes "never answers without citations" a hard
 constraint the model can't talk its way around. This guardrail is
 retrieval-technology-agnostic — it works identically whether search_docs
 is backed by FTS5 or, as built here, HybridCypherRetriever.
 - After the run, plain Python (not the LLM) checks confidence against
 config.qa_assistant.confidence_threshold. Clears the bar → post the
 answer with citations. Doesn't clear it, or search_docs never returned
 anything → don't post; call escalate_to_maintainer instead. Mirrors
 merge_policy.py: the model explains, the policy check decides.
 - Skill doc skills/kyverno/qa-assistant.md (frontmatter + structure
 matching the three existing skill docs exactly — see §7) instructs the
 agent to search before answering, cite only real results, and prefer
 escalation over a guess.

 Slack adapter — Slack Bolt for Python, not hand-rolled

 - requirements.txt: slack-bolt (pulls in slack_sdk transitively).
 - src/slack_app.py — builds the Bolt App from SLACK_BOT_TOKEN/
 SLACK_SIGNING_SECRET. Bolt handles signature verification, the
 url_verification challenge, event routing, and the 3-second-ack
 requirement itself (ack immediately, process async) — none of that is
 hand-rolled.
   - @app.event("app_mention") — converts the Slack event into an
 Event(source="slack", type="app_mention", ...) and calls the existing
 dispatch() (§1), keeping every entry point going through the same
 registry and audit trail as GitHub events.
   - @app.assistant — Slack's native Assistant class, so this shows up in
 Slack's AI Assistant side panel rather than as a bot posting in a
 channel thread: sets suggested prompts on thread_started (e.g. "Does
 Kyverno support mutate policies for Ingress?"), and on user_message
 calls qa_assistant.answer_question(..., source="slack_assistant"),
 streaming status/partial output back via Bolt's assistant say/update
 helpers. (Exact Bolt Assistant method names get confirmed against
 current slack-bolt docs during implementation — the surface is new
 enough that pinning exact signatures here would be guessing.)
   - @register_handler("app_mention") in qa_assistant.py extracts the
 question text + channel/thread ts, calls answer_question(...), posts
 the reply via bolt_app.client.chat_postMessage.
 - src/main.py: mount via
 slack_bolt.adapter.fastapi.SlackRequestHandler at a single route —
 @app.post("/slack/events") async def slack_events(req): return await handler.handle(req).
 Bolt does not replace FastAPI; it hands control to the same
 qa_assistant.answer_question() used everywhere else.
 - src/tools/slack_tools.py stays, but thinner than originally planned: just
 the plain post_message/post_thread_reply wrappers around
 bolt_app.client.chat_postMessage, reused by the Security Agent (§5) for
 its private-channel report — signature verification is no longer
 hand-rolled here, Bolt owns that.

 GitHub Discussions adapter

 Unchanged from the original design — GitHub Discussions has no REST API,
 only GraphQL:
 - src/tools/discussion_tools.py — raw httpx.post("https://api.github.com/graphql", ...)
 (httpx already a dependency) implementing add_discussion_comment(discussion_id, body).
 - discussion/discussion_comment webhook events flow through the existing
 /webhook endpoint once added to EXTERNAL_ID_FIELD (§1) — no new
 endpoint needed. New @register_handler("discussion_comment") in
 qa_assistant.py calls answer_question(..., source="github_discussion").
 - Requires the GitHub App gain the discussions: write permission —
 flagged since it's an install-time change outside this repo's code.

 Tests

 tests/test_qa_assistant.py (the propose_answer citation-rejection logic
 and the confidence-threshold gate are pure Python — fully testable offline,
 no query() mocking, no live Neo4j needed since the retriever is mocked at
 the search_docs boundary), tests/test_doc_retriever.py (against a
 locally-running test Neo4j — see Verification), tests/test_discussion_tools.py
 (GraphQL request shape, mocked via respx, already a dev dependency). Slack
 adapter tests use Bolt's own test helpers / a mocked bolt_app.client
 rather than hand-rolled signature tests.

 ---
 4. Coach Agent (src/agents/coach.py)

 Fires on pull_request.opened from a non-bot author (§1 gating). New
 build_coach_tool_server in github_tools.py: get_pr_diff read-only + a
 comment_on_pr analogue — no merge/approve capability exists in this server
 at all, same "capability doesn't exist" pattern as allow_merge. Loads a
 new skills/kyverno/coach.md skill doc (contributor-mentorship tone, points
 at AGENTS.md/per-package docs for the touched path, flags obvious
 test-coverage gaps and DCO sign-off issues) and posts one encouraging,
 specific comment. Deterministic pre-check (not LLM): does the PR's diff
 touch any path under safe_boundaries.restricted_paths? If so, the comment
 says so explicitly before the LLM's stylistic feedback — reuses
 pr_files/SafeBoundaries already in github_tools.py/config.py. (A
 tree-sitter code-structure graph for deeper "what does this diff touch
 structurally" queries is a fast-follow, not this pass — see end of doc.)

 ---
 5. Security Agent (src/agents/security_agent.py)

 Fires on issues events carrying the security label (§1). Per Dimension 2:
 "no access to public comment posting... a private report that goes only to
 maintainers." Its tool server (new security_tools.py) deliberately has
 no comment_on_issue tool at all — the only tool offered is
 file_private_report, which writes the report to the audit log
 (dashboard is maintainer-only/local, never public) and, if
 SLACK_BOT_TOKEN is set, posts to config.security_agent.private_slack_channel
 via slack_tools.post_message — never the public Slack channel the Q&A bot
 uses. No GitHub Security Advisory drafting in this pass (needs a separate
 elevated GitHub App permission) — flagged as a natural next step.

 ---
 6. Pattern Agent (src/agents/pattern_agent.py) + cron ingress

 main.py gains POST /internal/cron/{job} (job ∈
 {"pattern-agent", "doc-index-refresh"}), protected by a shared-secret
 header (X-Cron-Secret vs. new CRON_SECRET env var). Builds
 Event(source="cron", type=job, ...) and dispatches like the webhook path.
 Two new .github/workflows/*.yaml (schedule: cron trigger, curl to this
 endpoint). doc-index-refresh re-runs the §3 pipeline (crawl → GraphRAG
 index → Neo4j load) on a schedule so the doc graph doesn't go stale.

 Clustering is deterministic, not LLM (Dimension 6 precedent): pull
 issues opened in the last pattern_agent.lookback_days via the GitHub
 search API, group by Jaccard/token-overlap similarity on title+body (stdlib
 only) plus shared labels, keep clusters of size ≥ min_cluster_size. The
 LLM's only job, once a cluster is deterministically identified, is to draft
 the tracking issue's natural-language summary citing the linked issue
 numbers — via a file_tracking_issue tool (github_tools.py,
 repo.create_issue(...)).

 ---
 7. Reproduction Agent (src/agents/reproduction.py) — trigger + ingest only

 Scoped down deliberately (most infra-heavy piece). Two halves:

 1. Trigger: trigger_reproduction(issue_number, external_id, parent_run_id)
 extracts policy/resource manifests from the issue body via a new,
 deterministic extract_manifests_from_issue_body() (new
 src/agents/issue_reproduction_fields.py — splits on ---, classifies
 each YAML doc by kind: (Cluster)?Policy → policy manifest, else →
 resource manifest), then calls a new plain function in
 github_tools.py, dispatch_reproduction_workflow(gh, repo, inputs) →
 repo.get_workflow(config.reproduction_agent.workflow_file).create_dispatch(...).
 Writes one AuditEntry (action_result="dispatched, pending completion",
 parent_run_id=<triage's entry id>).
 2. Ingest: new .github/workflows/reproduce-issue.yaml (KinD setup →
 Helm-install Kyverno at the reported version → apply extracted manifests →
 capture admission response/policy report/events → post the result
 directly to the issue via gh issue comment inside the Action, using
 the Action's own GITHUB_TOKEN — no round-trip through kyctrl needed for
 posting). New @register_handler("workflow_run") in reproduction.py
 writes a second AuditEntry (success/failed: <conclusion>,
 parent_run_id=<dispatch entry id>) from the webhook's completion signal.

 This mirrors how pr_hygiene/codegen_gate are already "designed,
 narrative-only" in this repo: the trigger-and-audit Python is fully testable
 offline; the KinD YAML itself is only verifiable live in GitHub Actions.

 ---
 8. Skill docs (skills/kyverno/*.md)

 New: qa-assistant.md, coach.md, security-agent.md, pattern-agent.md.
 Each matches the exact convention in the three existing docs: YAML
 frontmatter (skill:, loaded_by:, grounded_in: — real sources:
 kyverno.io URLs, the Discussions Q&A category, AGENTS.md, Kyverno's actual
 label taxonomy), # <Title> — Kyverno H1, an opening paragraph stating it's
 system_prompt context grounded in real project shape, H2 sections with
 concrete tables/rules, closing example-output blockquotes.

 ---
 9. Config, env, docs

 - src/config.py / .github/ai-maintainer.yaml: new Pydantic models
 QaAssistantPolicy (incl. default_kyverno_version: "latest"),
 PatternAgentPolicy, CoachAgentPolicy, SecurityAgentPolicy,
 ReproductionAgentPolicy. New workflows keys, all false by default:
 qa_assistant, pattern_agent, coach_agent, security_agent,
 reproduction_agent. issue_triage.exclusion_labels gains security.
 - .env.example: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET,
 NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, CRON_SECRET; also add the
 pre-existing undocumented AUDIT_DB_PATH while touching this file.
 - Dockerfile: no Playwright/Chromium install by default — only needed
 if someone opts into crawl_docs.py --js-render locally, documented in
 docs/TESTING.md rather than baked into the image.
 - README.md: update the feature-status table; add a row for the
 multi-agent fan-out and the Neo4j-backed Q&A bot; note the new neo4j
 service in the quickstart.
 - docs/TESTING.md: new section on smoke-testing /slack/events (Bolt
 provides its own local-testing guidance — link it), /internal/cron/*,
 and the doc-graph pipeline (crawl_docs.py → run_graphrag_index.py →
 graphrag_to_neo4j.py against a local Neo4j).

 ---
 Deferred, not this pass

 Per your sequencing choice, these land as fast-follow work on the same Neo4j
 instance once the Q&A path above is proven:

 - Tree-sitter code-structure graph — parses the Kyverno Go codebase into
 an AST-derived graph in Neo4j, exposed via MCP, so Coach Agent can ask
 "what does this diff touch structurally" and Pattern Agent can ask "which
 components are involved in this week's issues."
 - Graphiti temporal memory — every agent action/decision/override as a
 Graphiti episode with validity windows, in its own Neo4j namespace,
 queried by agents before reasoning. This is Dimension 3 (Memory and
 Learning), not Dimension 2 — populating audit.py's already-reserved
 memory_refs column is the natural landing point when this is built.

 ---
 Verification

 - python3 -m pytest tests/ -q — new tests follow the existing
 patch_runtime()-per-test-file convention and stay offline; the Q&A
 agent's core safety logic (propose_answer citation rejection, confidence
 gate) is tested with search_docs mocked, not a live Neo4j.
 - docker compose up neo4j + tests/test_doc_retriever.py — the one test
 module that needs a real (local, ephemeral) Neo4j, run separately from the
 offline suite, mirroring how docs/TESTING.md already separates
 network-requiring tiers from Tier 0.
 - Manual: run the two-step pipeline (crawl_docs.py → build_doc_index.py)
 against a few real kyverno.io pages, confirm the citation URLs
 search_docs returns are correct and specific (not just the homepage),
 and confirm kyverno_version tags land correctly on a versioned page.
 Re-run crawl_docs.py + build_doc_index.py a second time unchanged and
 confirm it's fast (proving the incremental-update behavior actually
 skips unchanged pages, not just claiming to).
 - Manual (once you provide Slack credentials): use slack-bolt's local dev
 tooling (or ngrok) to hit /slack/events for real, and a Tier-2-style
 direct call to qa_assistant.answer_question(...) — both documented as
 new subsections of docs/TESTING.md.
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌