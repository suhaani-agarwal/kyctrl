from src.agents.issue_fields import missing_bug_fields, parse_issue_sections, uses_webhook_template

COMPLETE_BODY = """\
### Kyverno Version

1.18.0

### Description

Policy fails to mutate pods when namespaceSelector is set.

### Steps to reproduce

1. Apply the policy below

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: add-label
```

### Expected behavior

The label should be added to the pod.
"""

MISSING_MANIFEST_BODY = """\
### Kyverno Version

1.18.0

### Description

Something is broken.

### Steps to reproduce

1.

### Expected behavior

It should work.
"""

WEBHOOK_BODY = """\
### Kyverno Version

1.18.0

### Kubernetes Version

1.31.x

### Kubernetes Platform

EKS

### Description

Bug happens.
"""


def test_parse_issue_sections_splits_on_headers():
    sections = parse_issue_sections(COMPLETE_BODY)
    assert sections["kyverno version"] == "1.18.0"
    assert "namespaceSelector" in sections["description"]


def test_parse_issue_sections_handles_empty_body():
    assert parse_issue_sections(None) == {}
    assert parse_issue_sections("") == {}


def test_missing_bug_fields_none_missing_on_complete_report():
    required = ["kyverno-version", "bug-description", "bug-reproduce-steps", "bug-expectations"]
    assert missing_bug_fields(COMPLETE_BODY, required) == []


def test_missing_bug_fields_flags_placeholder_repro_steps():
    required = ["kyverno-version", "bug-description", "bug-reproduce-steps", "bug-expectations"]
    missing = missing_bug_fields(MISSING_MANIFEST_BODY, required)
    assert "bug-reproduce-steps" in missing


def test_missing_bug_fields_flags_absent_field_entirely():
    required = ["kyverno-version", "bug-description", "bug-reproduce-steps", "bug-expectations"]
    missing = missing_bug_fields("### Kyverno Version\n\n1.18.0\n", required)
    assert "bug-description" in missing
    assert "bug-reproduce-steps" in missing


def test_uses_webhook_template_detects_k8s_version_section():
    assert uses_webhook_template(WEBHOOK_BODY) is True
    assert uses_webhook_template(COMPLETE_BODY) is False
