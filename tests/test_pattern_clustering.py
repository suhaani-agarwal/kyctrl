from src.agents.pattern_clustering import ClusterableIssue, cluster_issues, similarity


def test_similarity_high_for_near_duplicate_titles():
    a = ClusterableIssue(1, "ClusterPolicy namespaceSelector not matching", "generate rule fails", {"bug"})
    b = ClusterableIssue(2, "namespaceSelector not matching in ClusterPolicy", "generate rule also fails here", {"bug"})
    assert similarity(a, b) > 0.25


def test_similarity_low_for_unrelated_issues():
    a = ClusterableIssue(1, "CLI validate command crashes on empty file", "", {"type:cli"})
    b = ClusterableIssue(2, "Webhook TLS certificate rotation fails", "", {"bug"})
    assert similarity(a, b) < 0.25


def test_cluster_issues_groups_related_and_leaves_singletons_out():
    issues = [
        ClusterableIssue(1, "ClusterPolicy namespaceSelector not matching", "generate rule fails", {"bug"}),
        ClusterableIssue(2, "namespaceSelector not matching in ClusterPolicy", "generate rule also fails here", {"bug"}),
        ClusterableIssue(3, "namespaceSelector matching broken for generate", "same symptom as the others", {"bug"}),
        ClusterableIssue(4, "CLI validate command crashes on empty file", "totally unrelated", {"type:cli"}),
    ]
    clusters = cluster_issues(issues, min_cluster_size=2, similarity_threshold=0.2)

    assert len(clusters) == 1
    assert {i.number for i in clusters[0]} == {1, 2, 3}


def test_cluster_issues_empty_input_returns_no_clusters():
    assert cluster_issues([], min_cluster_size=2) == []


def test_cluster_issues_deterministic_regardless_of_input_order():
    issues = [
        ClusterableIssue(1, "namespaceSelector not matching in ClusterPolicy", "generate rule also fails here", {"bug"}),
        ClusterableIssue(2, "ClusterPolicy namespaceSelector not matching", "generate rule fails", {"bug"}),
    ]
    reversed_issues = list(reversed(issues))

    a = cluster_issues(issues, min_cluster_size=2, similarity_threshold=0.2)
    b = cluster_issues(reversed_issues, min_cluster_size=2, similarity_threshold=0.2)

    assert {i.number for i in a[0]} == {i.number for i in b[0]}
