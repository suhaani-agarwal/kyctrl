"""Parses GitHub issue-form bodies and checks bug reports for
completeness, per skills/kyverno/issue-triage.md.

GitHub renders each form field as a `### <label>` markdown header followed
by the answer — using the field's *label*, not its *id*. `FIELD_HEADERS`
is the real id → label mapping read directly from
`.github/ISSUE_TEMPLATE/bug-{other,cli,webhook}.yaml` in kyverno/kyverno.
"""

from __future__ import annotations

import re

_SECTION_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_MANIFEST_RE = re.compile(r"apiVersion\s*:.*\n.*kind\s*:", re.IGNORECASE)
_EMPTY_PLACEHOLDERS = {"", "1.", "_no response_"}

FIELD_HEADERS = {
    "kyverno-version": "Kyverno Version",
    "bug-description": "Description",
    "bug-reproduce-steps": "Steps to reproduce",
    "bug-expectations": "Expected behavior",
    "k8s-version": "Kubernetes Version",
    "k8s-platform": "Kubernetes Platform",
}


def parse_issue_sections(body: str | None) -> dict[str, str]:
    """{"kyverno version": "1.18.0", "steps to reproduce": "1. ...", ...}
    keyed by lowercased header, since that's all we need for lookups."""
    if not body:
        return {}
    parts = _SECTION_RE.split(body)
    sections: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        header = parts[i].strip().lower()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        sections[header] = content
    return sections


def _is_meaningful(content: str) -> bool:
    return content.strip().lower() not in _EMPTY_PLACEHOLDERS


def _has_manifest(content: str) -> bool:
    return bool(_MANIFEST_RE.search(content))


def uses_webhook_template(body: str | None) -> bool:
    """Detected by field presence, not a label — bug-webhook.yaml is the
    only template with a Kubernetes Version section."""
    return FIELD_HEADERS["k8s-version"].lower() in parse_issue_sections(body)


def missing_bug_fields(body: str | None, required_fields: list[str]) -> list[str]:
    sections = parse_issue_sections(body)
    missing = []
    for field_id in required_fields:
        header = FIELD_HEADERS.get(field_id, field_id).lower()
        content = sections.get(header, "")
        if not _is_meaningful(content):
            missing.append(field_id)
            continue
        if field_id == "bug-reproduce-steps" and not _has_manifest(content):
            missing.append(field_id)
    return missing
