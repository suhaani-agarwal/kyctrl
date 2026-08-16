"""Process-wide Neo4j driver singleton — the `runtime.py` pattern (a single
`@lru_cache`d accessor other modules import, never construct their own
client) applied to the graph store backing the Q&A assistant's document
index (`tools/doc_retriever.py`, LightRAG's `Neo4JStorage` backend) and,
once built, the fast-follow tree-sitter code graph and Graphiti temporal
memory — all three share this one instance, kept apart by node
labels/namespaces rather than separate databases, per the plan.
"""

from __future__ import annotations

import os
from functools import lru_cache

from neo4j import Driver, GraphDatabase


@lru_cache
def get_neo4j_driver() -> Driver:
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    # NEO4J_USERNAME (not NEO4J_USER) — matches the env var name LightRAG's
    # own Neo4JStorage backend reads directly, so one .env value serves both
    # this driver and LightRAG's.
    user = os.environ.get("NEO4J_USERNAME", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    return GraphDatabase.driver(uri, auth=(user, password))
