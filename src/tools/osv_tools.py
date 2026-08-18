"""OSV.dev vulnerability lookup — a new deterministic input to
merge_policy.evaluate(), called directly and synchronously by that module,
never exposed to the LLM as a tool. Same rationale as pr_checks_all_green
in github_tools.py: this is a policy check, not a judgment call.

OSV (https://osv.dev) is fully open source (Apache 2.0, OpenSSF-backed),
free, and needs no API key or account — unlike commercial supply-chain
scanners (Socket.dev included), which is why it's the fit here. It also
covers the `Go` ecosystem directly, which is the actual footprint of
Kyverno's Dependabot traffic (`gomod` + `github-actions`), where
npm/PyPI-centric scanners add little. It's the same vulnerability database
`govulncheck` itself queries.

Trade-off, stated plainly: OSV is a known-vulnerability (CVE/GHSA/OSV-id)
database, not a behavioral/typosquat detector — it answers "does this exact
version have a disclosed vulnerability," not "does this package's install
script do something suspicious." For Go modules and GitHub Actions refs
(not npm/PyPI), that's the right free/open tool for the job.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

OSV_API_URL = "https://api.osv.dev/v1/query"

# OSV ecosystem tags relevant to Kyverno's two Dependabot ecosystems
# (see .github/dependabot.yml / skills/kyverno/dependabot-policy.md).
_GO_ECOSYSTEM = "Go"
_GITHUB_ACTIONS_ECOSYSTEM = "GitHub Actions"


class OsvCheckUnavailable(Exception):
    """Raised on any non-2xx response, timeout, connection failure, or
    unparseable response body. Callers must treat this the same way
    merge_policy.evaluate() already treats GithubException from
    pr_checks_all_green: hold, don't guess "clean"."""


@dataclass
class OsvVulnerability:
    id: str  # e.g. "GHSA-xxxx-xxxx-xxxx" or "GO-2024-1234"
    summary: str | None = None


def infer_ecosystem(dependency_name: str) -> str | None:
    """Best-effort mapping from a Dependabot dependency name to the OSV
    ecosystem tag to query it under. Go module paths are domain-qualified
    (a dot in the first path segment, e.g. "k8s.io/client-go",
    "github.com/sigstore/cosign/v3"); GitHub Actions refs are bare
    "owner/repo" (e.g. "actions/checkout"). Returns None — not a guess —
    when neither shape matches; callers must treat that the same as any
    other "can't verify" gap, not as "no ecosystem == skip the check"."""
    if "/" not in dependency_name:
        return None
    first_segment = dependency_name.split("/", 1)[0]
    return _GO_ECOSYSTEM if "." in first_segment else _GITHUB_ACTIONS_ECOSYSTEM


def check_package_vulnerabilities(
    ecosystem: str, package: str, version: str, *, timeout: float = 10.0
) -> list[OsvVulnerability]:
    """Queries OSV.dev for known vulnerabilities affecting this exact
    package+version+ecosystem. Empty list means clean. Raises
    OsvCheckUnavailable on any failure to reach or parse the API — an
    empty list is only ever returned for a real "no known vulnerabilities"
    response, never as a stand-in for "couldn't check"."""
    try:
        resp = httpx.post(
            OSV_API_URL,
            json={"version": version, "package": {"name": package, "ecosystem": ecosystem}},
            timeout=timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise OsvCheckUnavailable(str(e)) from e
    try:
        data = resp.json()
    except ValueError as e:
        raise OsvCheckUnavailable(f"non-JSON response from OSV.dev: {e}") from e
    return [
        OsvVulnerability(id=v["id"], summary=v.get("summary"))
        for v in data.get("vulns", [])
        if isinstance(v, dict) and "id" in v
    ]
