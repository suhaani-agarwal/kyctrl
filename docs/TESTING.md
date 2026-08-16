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
62 tests, covering:

| File under test | What's actually being checked |
|---|---|
| `src/config.py` | the real `.github/ai-maintainer.yaml` parses and validates; bad values (`auto_merge: yolo`) are rejected; both kill-switch layers behave correctly in combination |
| `src/agents/merge_policy.py` | the deterministic merge/hold rule engine, tested against **real Kyverno PR title formats** (e.g. `chore(deps): bump github.com/sigstore/cosign/v3 from 3.1.2 to 3.1.3 (#17008)` — copied from your actual merged-PR history) |
| `src/agents/issue_fsm.py` | the label state machine only allows the transitions it's supposed to |
| `src/agents/issue_fields.py` | missing-field detection on real Kyverno issue-template bodies (webhook template vs. other, placeholder detection, manifest detection) |
| `src/audit.py` | writes/reads/stats aggregation on a real (temp) SQLite file |
| `src/tools/github_tools.py` | the plain data-access functions (`pr_checks_all_green`, `pr_age_minutes`, etc.) against mock PR objects |
| `src/main.py` | HMAC signature verification, webhook dispatch routing, JSON API shape |

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

## Quick-reference: what's blocked on what, right now

| Capability | Needs | Status as of this doc |
|---|---|---|
| Rule engines, FSM, config validation | nothing | ✅ works, tested |
| Raw SDK call | `ANTHROPIC_API_KEY` | ✅ confirmed working |
| Dashboard UI (empty state) | nothing | ✅ works |
| Dashboard kill-switch toggle, any GitHub-touching endpoint | GitHub auth resolvable | ❌ blocked — `GITHUB_APP_ID` unset and no `GITHUB_PAT` fallback |
| Direct agent invocation (Tier 2) | `GITHUB_PAT` + `TARGET_REPO`, or a finished GitHub App | ❌ needs the PAT step above |
| Full webhook-driven demo (Tier 3) | Tier 2 setup + `gh webhook forward` (or a finished GitHub App) | ❌ needs Tier 2 first |
