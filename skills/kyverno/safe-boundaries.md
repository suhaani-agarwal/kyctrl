---
skill: kyverno/safe-boundaries
loaded_by: every agent, before it is granted any tool
grounded_in:
  - kyverno/kyverno AGENTS.md ("API Design Rules", "Generated code")
  - kyverno/kyverno .github/dependabot.yml exclusion candidates
---

# Safe automation boundaries — Kyverno

What the agent may touch autonomously, and what always requires a human.
This is the source document for `.github/ai-maintainer.yaml`'s
`safe_boundaries` section — if the two ever disagree, this file is right
and the config should be updated to match, not the other way around.

## Always requires a human — no autonomous action, ever

- **`api/kyverno/v1/`** — Kyverno's `AGENTS.md` states new resource types
  must not be added to `v1` at all, and existing attributes can't be
  deleted/modified within a version. Any change here is an API-compat
  decision by definition.
- **`pkg/cosign/`, `pkg/notary/`** — image-signature verification. A wrong
  autonomous change (even a "safe-looking" dependency bump the rule engine
  would otherwise auto-merge) changes what Kyverno considers a validly
  signed image. These are also on the `dependabot.excluded_packages` list
  for the same reason.
- **Anything under `zz_generated.*` or `pkg/client/`** — per `AGENTS.md`,
  these are never hand-edited by anyone, human or agent; the only valid
  autonomous action here is *running* `make codegen-all-code` and
  reporting whether the result matches what's committed (the codegen gate,
  Phase 1c — narrative-only in this prototype).
- **Branch protection / merge into `main` or `release-*` directly** — not a
  policy choice, a physical impossibility: the GitHub App's installation
  token is subject to the same branch protection rules as any other
  credential. The agent's only path to changing `main` is the same one a
  human contributor uses — open a PR, get it merged through the normal
  merge API, which enforces required reviews and checks.

## Autonomous, within the rule engine's limits

- Dependency bumps (`go.mod`, `go.sum`, GitHub Actions pins) that clear
  every condition in `skills/kyverno/dependabot-policy.md` — patch/minor
  only, not on the exclusion list, CI green, no hold label.
- `config/crds/**` — generated CRD manifests, safe to regenerate and
  commit as part of the codegen gate (not implemented in this prototype,
  but scoped as autonomous here for when it is).
- Labels and comments on issues/PRs — always reversible, always logged,
  exactly the "comments, labels, draft PRs" the issue asks for.

## The rule, restated simply

If a change is **reversible by a single git revert or label removal** and
**mechanically verifiable** (CI passed, semver bump type is known, a
generated-file diff is empty), it can be autonomous. If reversing it needs
a value judgment — "was this API change actually backward compatible," "is
this cosign bump safe" — it doesn't matter how small the diff looks, it
goes to a human.
