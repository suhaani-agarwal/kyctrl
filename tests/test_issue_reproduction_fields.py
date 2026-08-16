from src.agents.issue_reproduction_fields import extract_manifests_from_issue_body

_BUG_BODY = """
### Kyverno Version

1.14.0

### Steps to reproduce

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-labels
spec:
  rules: []
---
apiVersion: v1
kind: Pod
metadata:
  name: test
spec:
  containers: []
```
"""


def test_extracts_policy_and_resource_manifests():
    result = extract_manifests_from_issue_body(_BUG_BODY)
    assert result.is_reproducible
    assert len(result.policy_manifests) == 1
    assert result.policy_manifests[0]["kind"] == "ClusterPolicy"
    assert len(result.resource_manifests) == 1
    assert result.resource_manifests[0]["kind"] == "Pod"


def test_no_manifests_is_not_reproducible():
    result = extract_manifests_from_issue_body("### Steps to reproduce\n\n1. do the thing\n2. see the bug\n")
    assert not result.is_reproducible
    assert result.policy_manifests == []
    assert result.resource_manifests == []


def test_malformed_yaml_is_skipped_not_raised():
    body = "```yaml\napiVersion: kyverno.io/v1\nkind: ClusterPolicy\n  bad indent: [unterminated\n```"
    result = extract_manifests_from_issue_body(body)
    assert result.policy_manifests == []


def test_empty_body_is_not_reproducible():
    result = extract_manifests_from_issue_body("")
    assert not result.is_reproducible
    result = extract_manifests_from_issue_body(None)
    assert not result.is_reproducible
