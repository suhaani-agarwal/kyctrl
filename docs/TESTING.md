# Testing kyctrl — what works, why, and how to verify it yourself

This doc is tiered: each tier needs strictly more setup than the last. Do
them in order — each one proves a real thing works before you add the next
piece of complexity.

---

## Tier 0 — Static verification (zero setup, works right now)

**What it proves:** the decision-making logic is correct, independent of
any LLM call or GitHub API call.

```bash
cd /Users/suhaaniagarwal/kyctrl
source .venv/bin/activate
python3 -m pytest tests/ -q
```

**Why this works without any credentials:** every test mocks the GitHub
objects (`unittest.mock.MagicMock`/`SimpleNamespace` standing in for
`PullRequest`/`Issue`) and never imports anything that calls the network.
110 tests, covering:

| File under test | What's actually being checked |
|---|---|
| `src/config.py` | the real `.github/ai-maintainer.yaml` parses and validates; bad values (`auto_merge: yolo`) are rejected; both kill-switch layers behave correctly in combination |
| `src/agents/merge_policy.py` | the deterministic merge/hold rule engine, tested against **real Kyverno PR title formats** (e.g. `chore(deps): bump github.com/sigstore/cosign/v3 from 3.1.2 to 3.1.3 (#17008)` — copied from your actual merged-PR history) |
| `src/agents/issue_fsm.py` | the label state machine only allows the transitions it's supposed to |
| `src/agents/issue_fields.py` | missing-field detection on real Kyverno issue-template bodies (webhook template vs. other, placeholder detection, manifest detection) |
| `src/agents/pattern_clustering.py` | the Pattern Agent's deterministic issue-similarity/clustering algorithm — no LLM, no network |
| `src/agents/issue_reproduction_fields.py` | the Reproduction Agent's policy/resource-manifest extraction from real fenced-YAML issue bodies |
| `src/agents/qa_assistant.py` | the Q&A bot's two safety-critical pure functions: `validate_citations` (rejects an empty or fabricated citation) and `decide_post_or_escalate` (the confidence-threshold gate) |
| `src/audit.py` | writes/reads/stats aggregation on a real (temp) SQLite file |
| `src/tools/github_tools.py` | the plain data-access functions (`pr_checks_all_green`, `pr_age_minutes`, etc.) against mock PR objects |
| `src/main.py` | HMAC signature verification, webhook dispatch **fan-out** (one event type → multiple independent handlers, one handler's exception doesn't block another's), cron-endpoint auth, JSON API shape |
| `src/agents/{coach,security_agent,pattern_agent,reproduction}.py` | kill-switch/disabled-workflow/not-applicable skip paths for each new agent — same shallow-but-real coverage as `dependabot`/`issue_triage` already had |

You can also poke the new pure logic directly, no test framework needed:

```bash
python3 -c "
from src.agents.pattern_clustering import ClusterableIssue, cluster_issues
issues = [
    ClusterableIssue(1, 'ClusterPolicy namespaceSelector not matching', 'generate rule fails', {'bug'}),
    ClusterableIssue(2, 'namespaceSelector not matching in ClusterPolicy', 'generate rule also fails', {'bug'}),
]
print(cluster_issues(issues, min_cluster_size=2))
"
```

You can also poke the rule engine directly, no test framework needed:

```bash
python3 -c "
from src.agents.merge_policy import parse_bump_title
print(parse_bump_title('chore(deps): bump k8s.io/client-go from 0.31.0 to 0.32.0 (#1)'))
"
# → ('k8s.io/client-go', 'minor')
```

---

## Tier 1 — The Claude Agent SDK is live (needs `ANTHROPIC_API_KEY` only)

**What it proves:** your API key works and the SDK's agent loop actually
runs — no GitHub involved yet.

```bash
python3 -c "
import asyncio
from dotenv import load_dotenv; load_dotenv()
from claude_agent_sdk import query, ClaudeAgentOptions

async def main():
    options = ClaudeAgentOptions(system_prompt='Reply with exactly one word.', max_turns=1)
    async for m in query(prompt='Say OK', options=options):
        print(type(m).__name__, getattr(m, 'result', getattr(m, 'content', '')))

asyncio.run(main())
"
```

**Already confirmed working** — I ran this against your key and got a
real `ResultMessage` back with `OK`. This is the same `query()` call both
agents make, just without tools attached.

**Why it works:** `claude-agent-sdk` reads `ANTHROPIC_API_KEY` from the
environment automatically (no explicit wiring needed in our code). I fixed
one real bug while checking this — nothing was calling `load_dotenv()`, so
your `.env` file was invisible to the app. `src/main.py` now loads it
before anything reads `os.environ`, so this only matters if you're running
a bare script like the one above (which is why it's `load_dotenv()`'d
explicitly there too).

---

## Tier 2 — A real agent run against a real PR/issue (needs GitHub auth)

This is the first tier that proves the *whole* thing — rule engine → SDK
→ real GitHub tool calls → audit log — end to end. It skips webhooks
entirely by calling the agent's entrypoint function directly, which is the
fastest possible dev loop (no webhook forwarding, no repo webhook config,
no GitHub App).

**Four real bugs were caught and fixed by actually running this tier — all
were things Tier 0/1 couldn't have caught, since neither exercises
`can_use_tool` or a live GitHub call:**
- `query(prompt=<str>, options=...)` now raises `ValueError` at run time
  when `can_use_tool` is set (every agent sets it) — the installed SDK
  requires streaming-mode input in that case. Fixed by
  `src/runtime.single_turn_prompt()`, a one-item async generator both
  agents now wrap their prompt string in before calling `query()`.
- Every `@tool` in `github_tools.py` (and `transition_issue_state` in
  `issue_triage.py`) previously let a `GithubException` — e.g. a 403 from
  a PAT missing a scope — propagate and crash the whole run. They now
  catch it and return `{"is_error": True, ...}`, so a single failed tool
  call surfaces to the model as a normal failure instead of ending the run.
- `merge_policy.evaluate()` had the same problem one layer lower: its CI
  check called `pr_checks_all_green()` directly, unguarded, *before* the
  SDK is even invoked. A 403 there crashed `handle_dependabot_pr` outright.
  Fixed to catch `GithubException` and return `hold`/`checks_unavailable` —
  consistent with the module's own "never guess, hold when ambiguous"
  rule for unparseable titles.
- **The Checks API 403 wasn't a missing PAT scope — it's a permanent GitHub
  limitation.** There is no "Checks" permission you can grant a
  fine-grained PAT, full stop; GitHub disabled that for PATs and only
  GitHub Apps can call it (confirmed via GitHub staff:
  github.com/orgs/community/discussions/129512). Fixed by switching
  `pr_checks_all_green`/`get_check_status` to the **Commit Statuses**
  API (`get_combined_status`) instead of Checks (`get_check_runs`) —
  "Commit statuses" *is* a real, grantable fine-grained PAT permission.
  Note this only reads statuses explicitly posted to that API — a plain
  GitHub Actions workflow creates Check Runs, not Statuses, so a repo with
  Actions-only CI and no explicit status-posting step will still show
  `total_count=0` (correctly held as not-green, not crashed).
- **The mid-run kill switch was not actually gating any GitHub tool call.**
  Both agents listed their tool names in `allowed_tools` with no `(...)`
  specifier — the SDK auto-approves those before `can_use_tool` is ever
  consulted (`CanUseToolShadowedWarning` at run time confirms this). Fixed
  by setting `tools=[]` (no built-in Bash/Read/Write/...) and
  `allowed_tools=[]` on both agents, so every tool call falls through to
  `can_use_tool`, which now also checks the tool name against an explicit
  `mcp__github__*`/`mcp__state__*` allow-list (`src/runtime.py`) instead of
  rubber-stamping anything the kill switch didn't block. This is the fix
  that makes "flip the kill switch mid-run and the next tool call is
  denied" — a claim in the build plan and README — actually true.

### What you need

1. **A demo repo you control.** Don't point this at `kyverno/kyverno` —
   you're not a maintainer there and can't merge PRs on it. Create
   `suhaani-agarwal/kyctrl-demo-target` (empty is fine to start).
2. **A fine-grained PAT**, scoped to just that repo:
   github.com/settings/personal-access-tokens/new →
   - Repository access: **only** `kyctrl-demo-target`
   - Permissions: **Pull requests** (Read and write), **Issues** (Read and
     write), **Contents** (Read and write — needed to squash-merge),
     **Checks** (Read-only — `get_check_status` reads check-runs on the PR's
     head commit via a separate scope from Pull requests; without it every
     call 403s), **Metadata** (Read-only, required)
3. Add to `.env`:
   ```
   GITHUB_PAT=<the token>
   TARGET_REPO=suhaani-agarwal/kyctrl-demo-target
   ```
   Leave `GITHUB_APP_ID` empty — `get_auth_from_env()` (in
   `src/tools/github_auth.py`) checks the App vars first, and only falls
   back to `GITHUB_PAT` if they're not both set. This is intentional: it's
   the same "PAT now, App-shaped interface for later" split from the build
   plan. 

### Testing the Dependabot agent

1. On `kyctrl-demo-target`, open any PR whose **author is literally
   `dependabot[bot]`** — easiest way: add a `.github/dependabot.yml` to
   the repo bumping something trivial (or add one outdated pinned
   dependency to a `go.mod`/`package.json` and wait for Dependabot to open
   a PR — can take a few minutes to a day, not great for a live test).
   **Faster for testing purposes:** temporarily add your own GitHub
   username to `dependabot.bot_usernames` in `.github/ai-maintainer.yaml`
   and open a PR yourself titled like a bump, e.g.  
   `chore(deps): bump github.com/foo/bar from 1.0.0 to 1.0.1` — the rule
   engine only reads the title, not the actual author-verification beyond
   the username check, so this is a legitimate way to test the logic
   without waiting on real Dependabot. (Revert the username addition
   before recording the real demo — the video should show it working on
   the real bot account.)
2. Run:
   ```bash
   python3 -c "
   import asyncio
   from dotenv import load_dotenv; load_dotenv()
   from src.agents.dependabot import handle_dependabot_pr
   entry = asyncio.run(handle_dependabot_pr(pr_number=1, external_id='gh-pr-1'))
   print(entry)
   "
   ```
3. **What you should see:** a live `rich`-rendered panel in your terminal
   showing the agent's reasoning stream, then either a real approval +
   squash-merge on the PR, or a comment explaining why it's held —
   depending on what `merge_policy.evaluate()` decided *before* the SDK
   was even called. Check the PR on GitHub to see the actual comment/merge.
   Then check `entry` — it's the full audit row, including
   `agent_reasoning_summary` (the model's own words) and `total_cost_usd`
   (real spend for that call).

### Testing the issue-triage agent

1. On `kyctrl-demo-target`, open an issue using Kyverno's real bug
   template — easiest: copy the body of one of
   `.github/ISSUE_TEMPLATE/bug-other.yaml` from the kyverno checkout into
   a new issue, deliberately leaving "Steps to reproduce" as just `1. `
   (no manifest) so you can see the missing-info path.
2. Apply the label `bug` to it manually (Kyverno's real template does this
   automatically; a manually-created issue on your demo repo won't unless
   you add the label yourself).
3. Run:
   ```bash
   python3 -c "
   import asyncio
   from dotenv import load_dotenv; load_dotenv()
   from src.agents.issue_triage import handle_issue_event
   entry = asyncio.run(handle_issue_event(issue_number=1, external_id='gh-issue-1'))
   print(entry)
   "
   ```
4. **What you should see:** the agent notices the missing manifest via
   `issue_fields.missing_bug_fields()` (checked *before* the SDK call),
   posts a specific comment asking for it, and calls
   `transition_issue_state` — which adds the label `ai/needs-repro-info`
   to the issue. Refill the manifest field and remove that label manually
   (simulating a human/future-reproduction-agent doing it), then re-run
   with a complete body to see the `ai/ready-for-human` path instead.

### Testing the multi-agent fan-out (Dimension 2)

Before testing the four new agents individually, confirm the architectural
claim itself: one webhook event now reaches *more than one* independent
agent. This needs no credentials beyond what you already set up above.

```bash
python3 -c "
from src.main import app  # populates EVENT_HANDLERS via the side-effect imports
from src.events import EVENT_HANDLERS
for event_type, handlers in sorted(EVENT_HANDLERS.items()):
    print(event_type, '->', [h.__module__ + '.' + h.__name__ for h in handlers])
"
```

**What you should see:** `pull_request -> ['src.agents.dependabot.handle', 'src.agents.coach.handle']`
and `issues -> ['src.agents.issue_triage.handle', 'src.agents.security_agent.handle']`
— two independently-registered agents per event type, neither aware of the
other's existence in code. This is `src/events.py`'s `dispatch()` fanning
out via `asyncio.gather`, not an if/elif ladder.

Enable the new workflows in `.github/ai-maintainer.yaml` before testing any
of the four below — they default to `false`:

```yaml
workflows:
  coach_agent: true
  security_agent: true
  pattern_agent: true
  reproduction_agent: true
```

### Testing the Coach Agent

1. On `kyctrl-demo-target`, open a PR from **any account not in
   `dependabot.bot_usernames`** — i.e. just open one yourself normally, the
   opposite setup from the Dependabot test above.
2. Run:
   ```bash
   python3 -c "
   import asyncio
   from dotenv import load_dotenv; load_dotenv()
   from src.agents.coach import handle_coach_pr
   entry = asyncio.run(handle_coach_pr(pr_number=1, external_id='gh-pr-1'))
   print(entry)
   "
   ```
3. **What you should see:** one encouraging, specific comment on the PR —
   never a merge/approve action (`build_pr_tool_server(..., allow_merge=False)`
   means that tool isn't offered at all, structurally, not by instruction).
   If the diff touches a path in `safe_boundaries.restricted_paths`, the
   comment should call that out explicitly, first.

### Testing the Security Agent

1. On `kyctrl-demo-target`, open an issue and manually apply the `security`
   label (in real Kyverno this label only comes from the automated
   vuln-scanning workflow — you're simulating that here).
2. Run:
   ```bash
   python3 -c "
   import asyncio
   from dotenv import load_dotenv; load_dotenv()
   from src.agents.security_agent import handle_security_issue
   entry = asyncio.run(handle_security_issue(issue_number=1, external_id='gh-issue-1'))
   print(entry)
   "
   ```
3. **What you should see:** **no comment appears on the issue at all** —
   that's the point. Check `entry.agent_reasoning_summary` in your terminal
   for the model's assessment, and check `http://127.0.0.1:8000/api/audit`
   for the logged decision. If you've also set `SLACK_BOT_TOKEN` and a real
   `security_agent.private_slack_channel`, check that channel for the
   private report too.

### Testing the Pattern Agent

Needs at least two issues opened recently enough to fall inside
`pattern_agent.lookback_days` (7 by default), with genuinely similar
titles/bodies (see `pattern_clustering.py`'s Tier-0 test above for what
"similar enough" means).

1. Open two or three issues on `kyctrl-demo-target` with deliberately
   overlapping wording, e.g. "namespaceSelector not matching" and
   "ClusterPolicy namespaceSelector broken."
2. Run:
   ```bash
   python3 -c "
   import asyncio
   from dotenv import load_dotenv; load_dotenv()
   from src.agents.pattern_agent import handle_pattern_run
   entry = asyncio.run(handle_pattern_run(external_id='manual-pattern-run'))
   print(entry)
   "
   ```
3. **What you should see:** if a cluster was found, a new tracking issue on
   the repo linking the related issue numbers. If not,
   `entry.agent_decision == "no_clusters"` and nothing was created — check
   this first if nothing shows up; it usually means the issues weren't
   textually similar enough, not that anything's broken.

### Testing the Reproduction Agent (trigger half only)

The KinD half (`.github/workflows/reproduce-issue.yaml`) only runs inside
GitHub Actions — this tests the Python trigger decision, which is what's
actually testable locally.

1. Open a bug issue on `kyctrl-demo-target` with a real fenced YAML block
   containing a `ClusterPolicy`/`Policy` manifest (see
   `tests/test_issue_reproduction_fields.py` for a realistic body to copy).
2. Run:
   ```bash
   python3 -c "
   import asyncio
   from dotenv import load_dotenv; load_dotenv()
   from src.agents.reproduction import trigger_reproduction
   entry = asyncio.run(trigger_reproduction(issue_number=1, external_id='gh-issue-1-repro'))
   print(entry)
   "
   ```
3. **What you should see:** `entry.agent_decision == "dispatched"` if a
   policy manifest was found (this will try to actually call
   `workflow_dispatch` on `reproduce-issue.yaml` — install that file on the
   demo repo first, or expect `dispatched, pending completion` to actually
   be `dispatch_failed` if the workflow file doesn't exist there yet, which
   is a correct, honest result, not a bug). `"skipped"` with reason
   `"no policy manifest found in issue body"` if the body has no manifest —
   try this first without a manifest to see the skip path.

### Why this is safe to run against a repo you actually own

- The merge tool (`approve_and_merge_pr`) is **not offered to the model at
  all** unless `merge_policy.evaluate()` already returned `"merge"` — see
  `build_pr_tool_server(..., allow_merge=...)` in
  `src/tools/github_tools.py`. The model can talk all it wants; if the
  tool isn't in `allowed_tools`, it physically cannot call it.
- Every tool call is additionally gated by `can_use_tool` (in
  `src/runtime.py`), which re-reads both kill-switch layers before
  allowing it through — flip `enabled: false` in `.github/ai-maintainer.yaml`
  mid-run and the *next* tool call gets denied, not just the next agent
  invocation.
- Nothing here can touch `main` directly — the PAT/App token merges
  through GitHub's normal merge API, same as any contributor, so branch
  protection (once you turn it on) applies exactly the same way.

---

## Tier 3 — Full webhook-driven flow (what the demo video actually needs)

Tier 2 calls the agent functions directly from your terminal. The real
demo needs GitHub events to trigger the agent **automatically** — that's
the difference between "I can run this" and "this is a maintainer bot."

**Not `pysmee`** — it's unmaintained since 2019 and its SSE/JSON parsing
chokes on real `pull_request` payloads (they're 5–15 KB with the full PR
and repo objects nested in); if you hit `Unterminated string starting
at:...` that's this, not your setup. Use **`gh webhook forward`** instead —
an official extension from GitHub's own `cli` org, actively maintained, and
better for this project specifically: it talks to GitHub directly with no
third-party relay in between (a real security-relevant difference — a
`smee.io` channel is public and unauthenticated; nothing here is), and it
creates/tears down the repo's dev webhook for you, so there's no
Settings → Webhooks UI step either.

1. Start the server: `uvicorn src.main:app --reload --port 8000`
2. One-time: `gh extension install cli/gh-webhook`
3. In a second terminal:
   ```bash
   gh webhook forward \
     --repo=suhaani-agarwal/kyctrl-demo-target \
     --events=pull_request,issues,status \
     --url=http://127.0.0.1:8000/webhook \
     --secret=<same value as GITHUB_WEBHOOK_SECRET in .env>
   ```
   Keep this running — it creates a temporary dev webhook on the repo for
   the duration of the command and deletes it when you `Ctrl-C`. **Include
   `status`**, not just `pull_request,issues`: the `pull_request` event
   usually arrives and gets evaluated *before* CI finishes (a real race
   hit live against the demo repo — see the `handle_status` docstring in
   `agents/dependabot.py`), so without `status` a PR that only turns green
   afterward is never re-checked and holds forever.
4. Now opening a real PR/issue on the demo repo fires the whole pipeline
   automatically — watch it happen live in the `uvicorn` terminal and on
   `http://127.0.0.1:8000/` (the dashboard).

This is also where the **GitHub App** (vs. the PAT you used for Tier 2)
actually matters — an App's webhook config lives with the App itself and
auto-subscribes on every repo it's installed on, so you're not manually
forwarding per repo. For a one-repo demo, PAT + `gh webhook forward` is
equivalent and faster to set up; switch to the App only when you want the
"installation-scoped, auto-rotating token" story to be literally true in
the recording, not just architecturally true in the code.

---

## Tier 4 — Cron-triggered agents (Pattern Agent, doc-index refresh)

**What it proves:** the `/internal/cron/{job}` ingress works — the same
`Event` → `dispatch()` path as `/webhook`, just triggered by a schedule
instead of a GitHub delivery.

### What you need

Add to `.env`:
```
CRON_SECRET=<any string you make up>
```

### How to test

1. Start the server: `uvicorn src.main:app --reload --port 8000`
2. ```bash
   curl -i -X POST http://127.0.0.1:8000/internal/cron/pattern-agent \
     -H "X-Cron-Secret: <same value as CRON_SECRET>"
   ```
3. **What you should see:** `{"status": "accepted"}` immediately (the
   actual run happens in a background task, same as `/webhook`), then a new
   row in `http://127.0.0.1:8000/api/audit` a moment later. Try it with no
   header, or the wrong secret, first — you should get `401`. Try
   `/internal/cron/not-a-real-job` — you should get `404`.

This also confirms the fail-fast behavior is real: with `pattern_agent`
left `false` in `.github/ai-maintainer.yaml` (the shipped default), the
audit entry will show `agent_decision: "skipped"`, `"workflow disabled in
config"` — the endpoint accepted the trigger but the agent correctly
declined to act, exactly like every other kill-switch/disabled-workflow
path in this codebase.

---

## Tier 5 — The Q&A Assistant's doc graph (needs Neo4j + Voyage AI)

**What it proves:** the doc-crawl → LightRAG-index → Neo4j → retrieval
pipeline actually grounds answers in real content, and the citation
guardrail actually blocks an ungrounded answer.

This is the single biggest new setup requirement in the whole project — do
it in the sub-steps below, in order, and confirm each one before moving to
the next. Don't jump straight to `answer_question()`; if it fails, you want
to already know whether the problem is Neo4j, the crawl, the index, or the
agent itself.

### What you need

1. **A running Neo4j instance.**
   ```bash
   docker compose up -d neo4j
   ```
   Requires `NEO4J_PASSWORD` set in `.env` first — `docker-compose.yml`
   fails fast with a clear error if it isn't
   (`required variable NEO4J_PASSWORD is missing a value`). Check it's
   actually up: `http://localhost:7474` should load the Neo4j Browser.
2. **A Voyage AI API key** (Anthropic has no embeddings endpoint of its
   own — this is what `search_docs` uses to embed queries/documents).
   Get one at `dashboard.voyageai.com`, add to `.env`:
   ```
   VOYAGE_API_KEY=<your key>
   ```
3. `ANTHROPIC_API_KEY` (already set from Tier 1) — LightRAG uses this for
   entity extraction during indexing.
4. Enable the workflow: `workflows.qa_assistant: true` in
   `.github/ai-maintainer.yaml`.

### Step 1 — crawl a handful of pages first, not the whole site

```bash
python3 scripts/crawl_docs.py --limit 5
ls data/kyverno_docs/
```

**What you should see:** up to 5 `.md` files (fewer if some pages come back
too short and get skipped — check the log for
`"suspiciously little content"` warnings, which mean that particular page
needs `--js-render` instead, not that the whole crawl is broken) plus
whatever resolved `question`-labeled issues your `TARGET_REPO` has. Open
one of the `.md` files — confirm it has a `source_url:` frontmatter line
that's a real, working URL. That URL is what a citation will eventually
point back to, so it being wrong here means every downstream citation is
wrong too.

### Step 2 — build the index

```bash
python3 scripts/build_doc_index.py
```

**What you should see:** one `indexed <url> (version=...)` log line per
crawled file, no exceptions. This is the step that actually needs Neo4j and
both API keys reachable — if it hangs or errors, that's almost always one
of: Neo4j not actually up (`docker ps` to check), a bad/missing
`VOYAGE_API_KEY`, or `NEO4J_URI`/`NEO4J_USERNAME`/`NEO4J_PASSWORD` not
matching what `docker-compose.yml` started Neo4j with.

Re-run the same command a second time immediately after — it should finish
noticeably faster. That's the "incremental, not a full rebuild" property
(`tools/doc_retriever.py`'s docstring) actually holding, not just claimed.

### Step 3 — query the retriever directly, no agent involved yet

```bash
python3 -c "
import asyncio
from dotenv import load_dotenv; load_dotenv()
from src.tools.doc_retriever import search_docs

async def main():
    chunks = await search_docs('does kyverno support mutate policies', top_k=3)
    for c in chunks:
        print(c.source_url, '|', c.kyverno_version, '|', c.text[:120])

asyncio.run(main())
"
```

**What you should see:** real chunks with real `source_url`s pointing back
into what you crawled in Step 1. Empty results here means the index build
in Step 2 didn't actually put anything in that's relevant to this query —
try a query closer to the literal wording of a page you crawled.

### Step 4 — the full agent, including the citation guardrail

```bash
python3 -c "
import asyncio
from dotenv import load_dotenv; load_dotenv()
from src.agents.qa_assistant import answer_question
entry = asyncio.run(answer_question(
    'Does Kyverno support mutate policies for Ingress resources?',
    source='slack', thread_ref='general:123.456', external_id='manual-qa-1',
))
print(entry)
"
```

**What you should see:** `entry.agent_decision` is either `"answered"`
(confidence cleared `qa_assistant.confidence_threshold`, default `high`) or
`"escalated"` (it wasn't confident, or found nothing relevant — this is
correct behavior, not a failure). Without `SLACK_BOT_TOKEN` set yet, the
actual Slack post will no-op (logged, not raised) — the point of this step
is confirming the *decision* is sound; see Tier 6 for confirming it
actually posts somewhere.

Try lowering `qa_assistant.confidence_threshold` to `medium` and re-running
the same question — if it flips from `escalated` to `answered`, the gate is
working exactly as designed (`decide_post_or_escalate`, already covered by
a Tier 0 test — this confirms the same logic end-to-end with a real model
call).

---

## Tier 6 — Slack integration (needs a Slack app)

**What it proves:** the Bolt adapter is actually mounted and reachable, and
(once you have a real workspace) that a real Slack question round-trips
through the same `answer_question()` from Tier 5.

### What you need

1. Create a Slack app at `api.slack.com/apps` (from scratch, not a
   manifest — simplest for a first test):
   - **OAuth & Permissions** → Bot Token Scopes: `app_mentions:read`,
     `chat:write`, `assistant:write` (for the AI Assistant surface).
   - **Event Subscriptions** → Request URL: your server's
     `https://.../slack/events` (needs to be publicly reachable — use
     `ngrok http 8000` for local testing, then use the ngrok URL here).
     Subscribe to bot event: `app_mention`.
   - **Install to Workspace**, copy the **Bot User OAuth Token**
     (`xoxb-...`) and the **Signing Secret** (Basic Information page).
2. Add to `.env`:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_SIGNING_SECRET=...
   ```
3. Restart the server — check the startup log. The
   `"SLACK_BOT_TOKEN/SLACK_SIGNING_SECRET not set — Slack adapter not
   mounted"` warning should be **gone**; its absence is your confirmation
   the adapter mounted.

### How to test

1. `curl -i -X POST http://127.0.0.1:8000/slack/events -d '{}'` — before
   Slack credentials are set this returns `503`; after, it should return
   something other than `503` (Bolt will reject the malformed body with
   its own error, which is fine — you're confirming the route exists and
   Bolt owns it now, not that an empty POST is a valid Slack payload).
2. In the Slack workspace you installed the app to, invite the bot to a
   channel and `@mention` it with a real question.
3. **What you should see:** the bot replies in-thread with an answer +
   sources, or a "flagging this for a maintainer" message — driven by the
   exact same `answer_question()` you already tested directly in Tier 5.
   Check `/api/audit` — the entry's `trigger_event` should be `"slack"`.

---

## Tier 7 — GitHub Discussions integration

**What it proves:** the GraphQL posting path works and Discussions events
flow through the same `/webhook` endpoint as everything else.

### What you need

1. Enable Discussions on `kyctrl-demo-target` (repo Settings → Features).
2. Your Tier 2 PAT needs **Discussions: Read and write** added as a scope
   (fine-grained PATs support this; regenerate if you created yours before
   reading this). A production GitHub App needs the same permission added
   to its installation.
3. If you're using `gh webhook forward` from Tier 3, add
   `discussion_comment` to its `--events` list.

### How to test

1. Open a Discussion in the "Q&A" or "General" category on the demo repo.
2. Post a comment on it asking a real question.
3. **What you should see:** a reply comment on the discussion from the
   bot, or an `@`-mention escalation if `qa_assistant.escalation.github_maintainer_logins`
   is empty (the default) and it wasn't confident. Check `/api/audit` — the
   entry's `trigger_event` should be `"github_discussion"`.

If you'd rather test this without waiting on a real webhook delivery, call
the handler directly the same way Tier 2 does:
```bash
python3 -c "
import asyncio
from dotenv import load_dotenv; load_dotenv()
from src.agents.qa_assistant import handle_discussion_comment
from src.events import Event
event = Event(source='github', type='discussion_comment', external_id='manual-disc-1', payload={
    'discussion': {'node_id': '<real discussion node id, from the GraphQL API or the Discussions UI>'},
    'comment': {'body': 'Does Kyverno support mutate policies for Ingress resources?'},
})
asyncio.run(handle_discussion_comment(event))
"
```

---

## Tier 8 — Dimension 3: Graphiti temporal memory (reuses Tier 5's Neo4j + Voyage AI)

**What it proves:** every agent now remembers past runs and draws on that
memory before reasoning — `src/memory.py`'s `write_episode`/`search_context`
actually round-trip through a real Neo4j graph, not just unit-tested against
a mock. Do the sub-steps in order, same reasoning as Tier 5: isolate the new
piece (raw Graphiti wiring) before layering agents back on top of it, so a
failure tells you which layer broke.

### What you need

If you already did **Tier 5** (the Q&A doc graph), you have almost
everything already — Graphiti deliberately reuses the exact same Neo4j
instance and the exact same `VOYAGE_API_KEY`/`ANTHROPIC_API_KEY`, no new
infrastructure or secrets. Otherwise, do this fresh:

1. **A running Neo4j instance** — same one `docker-compose.yml`'s `neo4j`
   service already provides:
   ```bash
   docker compose up -d neo4j
   ```
   Needs `NEO4J_PASSWORD` set in `.env` (see Tier 5 if you haven't set this
   up yet). Confirm it's up: `http://localhost:7474` should load.
2. **`VOYAGE_API_KEY`** in `.env` — Graphiti's embedder, same provider and
   same key `doc_retriever.py` already uses (`dashboard.voyageai.com`).
3. **`ANTHROPIC_API_KEY`** (already set from Tier 1) — Graphiti's own
   `AnthropicClient` uses this for entity/edge extraction from each episode.
4. `pip install -r requirements.txt` — picks up `graphiti-core`.
5. Enable it: `memory.enabled: true` in `.github/ai-maintainer.yaml` (it
   ships `false` by default, same "off until its infra is set up"
   convention every Dimension-2 policy already uses).

**One thing that's different from every other agent capability in this
repo:** `security_agent.py` deliberately does **not** read or write this
memory, on purpose — see that module's docstring. If you test it in this
tier, its audit row correctly has `memory_refs: null` — that's not a bug.

### Step 1 — raw Graphiti wiring, no GitHub, no agent involved yet

```bash
python3 -c "
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv; load_dotenv()
from src.runtime import get_memory_client
from src.memory import write_episode, search_context

async def main():
    memory = get_memory_client()
    assert memory is not None, 'get_memory_client() returned None — check memory.enabled and NEO4J_URI/NEO4J_PASSWORD'

    # Normally done once by uvicorn's startup lifespan (src/main.py) — done
    # explicitly here since this script isn't running the server.
    await memory.build_indices_and_constraints()

    refs = await write_episode(
        memory,
        name='manual-smoke-test',
        episode_body='Dependabot bumped github.com/foo/bar from 1.0.0 to 1.0.1 and it was merged without incident.',
        source_description='manual smoke test',
        reference_time=datetime.now(timezone.utc),
    )
    print('write_episode returned', len(refs), 'refs:', refs)

    facts = await search_context(memory, query='github.com/foo/bar bump', limit=5)
    print('search_context found:', facts)

    await memory.close()

asyncio.run(main())
"
```

**What you should see:** `write_episode` returns a non-empty list of UUIDs
(a mix of node and edge UUIDs — Graphiti's Anthropic-backed extraction
turned the sentence into at least one entity), and `search_context` finds
at least one fact mentioning the bump. This step makes two real LLM/API
calls (Anthropic for extraction, Voyage for embeddings) plus a dedup pass
against the graph, so 5–20 seconds is normal, not a hang. If `refs` comes
back empty or this raises, the problem is in this raw layer — fix it here
before testing agents:

- **`ClientConnectorCertificateError` / SSL errors** — `src/memory.py`
  already sets `SSL_CERT_FILE` to `certifi`'s bundle at import time (aiohttp,
  which `voyageai.AsyncClient` uses, doesn't fall back to `certifi` the way
  `httpx`/`requests` do). If you're still seeing this, something upstream
  set `SSL_CERT_FILE` to a bad path first — `setdefault` won't override it.
- **`write_episode`/`search_context` log `... failed, continuing without
  memory: Error communicating with Voyage`, with no more detail** —
  this generic string is `voyageai`'s own async error wrapping
  (`api_requestor.py`: `except aiohttp.ClientError as e: raise
  APIConnectionError("Error communicating with Voyage") from e` — it drops
  the real cause from the message). The far more common real cause,
  confirmed live: **Voyage's no-payment-method tier caps you at 3
  requests/minute**, and `Graphiti.add_episode`/`search` fire several
  *concurrent* embedding calls internally (entity/edge dedup does multiple
  similarity searches in parallel) — easily enough to trip that limit on a
  single call. Fix: add a payment method at `dashboard.voyageai.com` →
  billing — "the free tokens (200M tokens for Voyage series 3) will still
  apply," a card just unlocks the higher RPM tier (takes a few minutes to
  take effect per Voyage's own message). To confirm this is actually what's
  happening rather than guessing, call the embedder directly and read
  `e.__cause__`, which isn't swallowed the way the wrapped message is:
  ```bash
  python3 -c "
  import asyncio
  from dotenv import load_dotenv; load_dotenv()
  from src.runtime import get_memory_client
  async def main():
      memory = get_memory_client()
      try:
          await asyncio.gather(*[memory.embedder.create(f'text {i}') for i in range(10)])
      except Exception as e:
          print(repr(e))
      await memory.close()
  asyncio.run(main())
  "
  ```
  A `RateLimitError(... http_status=429 ...)` here confirms it.
- **A bad/missing `VOYAGE_API_KEY` or unreachable Neo4j** — same as Tier 5's
  troubleshooting note; check `docker ps` and the key value first.

### Step 2 — a real agent noticing its own past run

This is the actual claim from `docs/kyctrl_extra_features.md` Dimension 3
made concrete: run the same agent twice on two *related* things and watch
the second run reference the first.

1. Open PR #1 on `kyctrl-demo-target` (same setup as Tier 2's Dependabot
   test) titled `chore(deps): bump github.com/foo/bar from 1.0.0 to 1.0.1`.
   Run:
   ```bash
   python3 -c "
   import asyncio
   from dotenv import load_dotenv; load_dotenv()
   from src.agents.dependabot import handle_dependabot_pr
   entry = asyncio.run(handle_dependabot_pr(pr_number=1, external_id='gh-pr-1'))
   print('memory_refs:', entry.memory_refs)
   "
   ```
   `entry.memory_refs` should be a non-null JSON string of UUIDs.
2. Open a **second** PR bumping the **same package** further, e.g.
   `chore(deps): bump github.com/foo/bar from 1.0.1 to 1.0.2`. Run the same
   command with `pr_number=2`.
3. **What you should see:** in the live terminal panel for PR #2, either a
   `search_memory(...)` tool call appears in the tool-call log, or the
   prompt context includes a line like `Relevant memory (past runs on this
   or similar dependencies): [...bump of github.com/foo/bar...]` —
   `agents/_shared.py::memory_search`'s prefetch, run before the model even
   starts reasoning. `entry.memory_refs` on PR #2's row should again be
   non-null. Check `http://127.0.0.1:8000/api/audit` — both rows show
   `memory_refs` as an actual JSON array (`entry_to_dict` decodes it), not
   a raw string.

Repeat the same idea for `issue_triage.py`: open two issues with similar
titles/bodies, run `handle_issue_event` on each (Tier 2 has the exact
command), and look for the `Relevant memory (past runs on similar issues):`
line and a non-null `memory_refs` on the second one.

### Step 3 — through the real webhook flow (if Tier 3 is already running)

If you've already got `gh webhook forward` running from Tier 3, this needs
no new machinery — restart `uvicorn` (so the startup `lifespan` hook in
`src/main.py` runs `build_indices_and_constraints()` once) with
`memory.enabled: true` set, then just open the same two-similar-PRs (or
two-similar-issues) pattern from Step 2 as real PRs/issues through the
GitHub UI instead of calling the function directly. Watch it happen live in
the `uvicorn` terminal and confirm the same `memory_refs`/prefetch-context
behavior on the dashboard.

### Step 4 — see it in the graph directly

Open Neo4j Browser (`http://localhost:7474`, same instance as Tier 5) and
run:
```cypher
MATCH (n:Episodic) RETURN n LIMIT 25
```
— one node per `write_episode` call (`name` property is the
`{repo}:pr-{n}` / `{repo}:issue-{n}` string every agent passes). Then:
```cypher
MATCH (n:Entity) RETURN n LIMIT 25
```
— the extracted entities; each carries `:Entity` plus whichever custom type
Graphiti classified it as (`:Package`, `:Issue`, `:Contributor`, etc. — see
`src/memory.py`'s `EntityTypes`). These are Graphiti's own labels, distinct
from whatever LightRAG's `Neo4JStorage` wrote for the Tier 5 doc graph in
the same database — a live check of the "shared instance, namespaced apart
by labels" claim in `src/graph.py`'s docstring, not just a comment.

### Step 5 — confirm it degrades gracefully when memory isn't there

The design's whole promise is "never breaks a run" — worth actually
checking, not just trusting the docstring:

```bash
docker compose stop neo4j
python3 -c "
import asyncio
from dotenv import load_dotenv; load_dotenv()
from src.agents.dependabot import handle_dependabot_pr
entry = asyncio.run(handle_dependabot_pr(pr_number=1, external_id='gh-pr-1-degraded'))
print(entry.action_result, entry.memory_refs)
"
docker compose start neo4j
```

**What you should see:** the run still completes normally
(`action_result` unaffected by Neo4j being down), `memory_refs` is `None`,
and the terminal/logs show a `write_episode(...) failed, continuing
without memory` warning rather than a crash. Same thing happens if you set
`memory.enabled: false` instead of stopping the container — either path is
"no memory this run," never "no run."

---

## Tier 8 — Temporal memory (Graphiti, Dimension 3)

**What it proves:** an agent's run actually gets remembered (`src/memory.py`'s
`write_episode`, via `AuditEntry.memory_refs`) and a later run on a related
PR/issue/cluster actually retrieves that memory back (`memory_search`) and
is shown it in its own prompt before it decides anything.

Reuses the exact same Neo4j instance and provider keys as Tier 5 — this is
not new infrastructure, just a new thing stored on it (Graphiti's own node
types, kept apart from LightRAG's doc-graph schema by construction — see
`src/memory.py`'s module docstring for why that's safe on Neo4j Community).
If Tier 5 already works, this tier is just a config flag away.

### What you need

Everything from Tier 5 (`docker compose up -d neo4j`, `VOYAGE_API_KEY`,
`ANTHROPIC_API_KEY`), plus:

```yaml
# .github/ai-maintainer.yaml
memory:
  enabled: true
```

Restart the server after flipping this — check the startup log for
`"Graphiti memory: indices/constraints ready"` (from `main.py`'s startup
hook). If you see `"memory.enabled is true but NEO4J_URI/NEO4J_PASSWORD
unset"` instead, that's `get_memory_client()` correctly refusing to guess —
go back and check `.env`.

### How to test

1. Run the Dependabot agent once on a real bump PR (Tier 2 steps) — same
   command as before, no code changes needed:
   ```bash
   python3 -c "
   import asyncio
   from dotenv import load_dotenv; load_dotenv()
   from src.agents.dependabot import handle_dependabot_pr
   entry = asyncio.run(handle_dependabot_pr(pr_number=1, external_id='gh-pr-1'))
   print('memory_refs:', entry.memory_refs)
   "
   ```
   **What you should see:** `entry.memory_refs` is a non-empty JSON list of
   UUID strings (not `None`) — confirmation the run's outcome actually got
   written as a Graphiti episode, not just logged to the audit table.
2. Open a *second* bump PR for the **same or a similarly-named package**,
   and run the same command with `pr_number=2`. Watch the terminal's
   streamed reasoning (`stream_agent_run`'s live panel) for the `Relevant
   memory (past runs on this or similar dependencies):` line in the
   prompt — it should now list a fact drawn from PR #1's episode, not
   `"none found"`.
3. Repeat with `coach.py`, `issue_triage.py`, or `pattern_agent.py`
   (`handle_coach_pr`, `handle_issue_event`, `handle_pattern_run`) — same
   idea, each one prefetches via `memory_search` before its prompt and
   writes via `memory_write` after.

### Why an empty `memory_refs` on the very first run is correct, not broken

The first PR/issue/cluster you ever run this against has nothing prior to
recall — `"none found"` in the prompt and a real (non-empty) `memory_refs`
afterward is the expected shape: nothing to retrieve yet, but this run
itself just became retrievable for the next one. If `memory_refs` stays
empty/`None` across multiple runs even after Tier 5 works fine on its own,
that's `write_episode` failing silently (by design — see its docstring) —
check the server log for a `write_episode(...) failed` warning to find out
why, rather than assuming memory is simply "off."

---

## Quick-reference: what's blocked on what, right now

| Capability | Needs | Status as of this doc |
|---|---|---|
| Rule engines, FSM, config validation, multi-agent fan-out, clustering, manifest extraction, citation/confidence gates | nothing | ✅ works, tested (110 tests) |
| Raw SDK call | `ANTHROPIC_API_KEY` | ✅ confirmed working |
| Dashboard UI (empty state) | nothing | ✅ works |
| Dashboard kill-switch toggle, any GitHub-touching endpoint | GitHub auth resolvable | ❌ blocked — `GITHUB_APP_ID` unset and no `GITHUB_PAT` fallback |
| Direct agent invocation — Dependabot, Issue Triage (Tier 2) | `GITHUB_PAT` + `TARGET_REPO`, or a finished GitHub App | ❌ needs the PAT step above |
| Direct agent invocation — Coach, Security, Pattern, Reproduction-trigger (Tier 2) | same PAT as above, `workflows.*: true` for each | ❌ needs the PAT step above |
| Full webhook-driven demo (Tier 3) | Tier 2 setup + `gh webhook forward` (or a finished GitHub App) | ❌ needs Tier 2 first |
| Cron ingress — Pattern Agent, doc-index refresh (Tier 4) | `CRON_SECRET` only | ❌ needs `CRON_SECRET` set |
| Q&A doc graph + retrieval + citation-grounded answers (Tier 5) | `docker compose up neo4j` + `VOYAGE_API_KEY` + `ANTHROPIC_API_KEY` | ❌ needs Neo4j running + Voyage key |
| Slack Q&A (Tier 6) | Tier 5 + a Slack app (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`) + public URL (ngrok for local) | ❌ needs a Slack app |
| GitHub Discussions Q&A (Tier 7) | Tier 5 + Discussions enabled on the repo + a PAT/App with Discussions write | ❌ needs Discussions enabled + scope |
| Reproduction Agent's KinD half | `.github/workflows/reproduce-issue.yaml` installed on the target repo (only runs inside GitHub Actions, not locally) | 🗺️ workflow file provided as a reference copy in this repo — install it on the target repo to actually exercise it |
| Dimension 3 — Graphiti temporal memory, all agents except `security_agent.py` (Tier 8) | same Neo4j + `VOYAGE_API_KEY`/`ANTHROPIC_API_KEY` as Tier 5, plus `memory.enabled: true` | ❌ needs Neo4j running + Voyage key + config flag flipped |
