"""Deterministic issue-clustering for the Pattern Agent — pure Python, no
LLM involved, same "Commit Graph Analysis... pure graph algorithms, no LLM
needed" precedent from kyctrl_extra_features.md Dimension 6. The LLM's only
job (in `pattern_agent.py`) is to draft the tracking issue's prose once a
cluster below is already deterministically identified — it never decides
*which* issues are related.

Similarity is a simple Jaccard token-overlap on title+body, with a bonus for
shared labels — no ML/embedding dependency, deliberately, since this is a
small-N-per-week problem (a handful of issues) where a fixed, auditable
formula is both sufficient and easy for a maintainer to sanity-check by eye.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "to", "of", "in", "on",
    "for", "with", "and", "or", "but", "this", "that", "it", "not", "does", "do", "when",
    "kyverno", "issue", "bug", "policy", "using", "use", "i", "we", "my", "our",
}
_LABEL_BONUS = 0.15


@dataclass
class ClusterableIssue:
    number: int
    title: str
    body: str
    labels: set[str] = field(default_factory=set)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS and len(t) > 2}


def similarity(a: ClusterableIssue, b: ClusterableIssue) -> float:
    tokens_a, tokens_b = _tokens(f"{a.title} {a.body}"), _tokens(f"{b.title} {b.body}")
    if not tokens_a or not tokens_b:
        score = 0.0
    else:
        score = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    if a.labels & b.labels:
        score += _LABEL_BONUS
    return min(score, 1.0)


def cluster_issues(
    issues: list[ClusterableIssue],
    *,
    min_cluster_size: int = 2,
    similarity_threshold: float = 0.25,
) -> list[list[ClusterableIssue]]:
    """Connected components over a similarity graph (edge when
    `similarity(a, b) >= similarity_threshold`), returning only components
    of at least `min_cluster_size`. Deterministic and order-independent —
    same input always produces the same clusters."""
    n = len(issues)
    adjacency: dict[int, set[int]] = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            if similarity(issues[i], issues[j]) >= similarity_threshold:
                adjacency[i].add(j)
                adjacency[j].add(i)

    visited: set[int] = set()
    clusters: list[list[ClusterableIssue]] = []
    for start in range(n):
        if start in visited:
            continue
        stack, component = [start], []
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            component.append(node)
            stack.extend(adjacency[node] - visited)
        if len(component) >= min_cluster_size:
            clusters.append([issues[i] for i in sorted(component)])

    return clusters
