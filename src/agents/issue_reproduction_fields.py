"""Deterministic manifest extraction for the Reproduction Agent — pure
Python, no LLM. Reuses the same heuristic named in
`skills/kyverno/issue-triage.md`'s completeness check: reporters paste
policy/resource manifests inline inside the `bug-reproduce-steps` section as
fenced YAML blocks containing `apiVersion`/`kind`, since Kyverno's bug
templates don't have separate policy/resource fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

_POLICY_KINDS = {"Policy", "ClusterPolicy", "PolicyException", "CleanupPolicy", "ClusterCleanupPolicy"}


@dataclass
class ExtractedManifests:
    policy_manifests: list[dict] = field(default_factory=list)
    resource_manifests: list[dict] = field(default_factory=list)

    @property
    def is_reproducible(self) -> bool:
        """A reproduction attempt needs at least one policy manifest — a
        resource with no policy applied to it isn't a reproducible bug
        report, it's just a YAML file."""
        return bool(self.policy_manifests)


def _iter_fenced_yaml_blocks(body: str):
    lines = (body or "").splitlines()
    in_block = False
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_block:
                yield "\n".join(current)
                current = []
                in_block = False
            else:
                in_block = True
            continue
        if in_block:
            current.append(line)
    if in_block and current:
        # Unterminated fence at EOF — still worth trying to parse.
        yield "\n".join(current)


def extract_manifests_from_issue_body(body: str) -> ExtractedManifests:
    """Splits every fenced code block in the issue body on `---`, parses
    each document as YAML, and classifies it by `kind`: a Kyverno policy
    kind -> policy manifest, anything else with an `apiVersion`/`kind` pair
    -> resource manifest. Malformed YAML and non-manifest fenced blocks
    (e.g. a pasted `kubectl` command) are silently skipped, never raised —
    this function's job is "find what's there," not "validate the report,"
    which `issue_fields.missing_bug_fields` already does separately."""
    result = ExtractedManifests()
    for block in _iter_fenced_yaml_blocks(body):
        for doc_text in block.split("\n---"):
            if "apiVersion" not in doc_text or "kind" not in doc_text:
                continue
            try:
                docs = list(yaml.safe_load_all(doc_text))
            except yaml.YAMLError:
                continue
            for doc in docs:
                if not isinstance(doc, dict) or "kind" not in doc:
                    continue
                if doc["kind"] in _POLICY_KINDS:
                    result.policy_manifests.append(doc)
                elif "apiVersion" in doc:
                    result.resource_manifests.append(doc)
    return result
