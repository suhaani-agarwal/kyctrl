"""Dimension 3 — Graphiti temporal memory (`docs/kyctrl_extra_features.md`).

Every agent's final `audit.write(...)` records *what* happened. This module
is what lets an agent also remember it: `write_episode` turns a run's
outcome into a Graphiti episode (extracted into typed, timestamped
nodes/edges), `search_context` turns a query into the facts most relevant
to it. `src/agents/_shared.py`'s `memory_search`/`memory_write` are the
actual call sites every agent uses — this module only owns construction and
the two raw operations, same split `audit.py` has between `AuditWriter` and
the module-level helpers that use it.

**Same Neo4j instance as `src/graph.py`/`tools/doc_retriever.py`, on
purpose, not a new one.** `src/graph.py`'s docstring already commits to
"kept apart by node labels/namespaces rather than separate databases, per
the plan" — and that's not just a style choice here, it's load-bearing:
`Graphiti.add_episode` re-points its driver at a *different Neo4j database*
whenever `group_id` differs from the driver's current database
(`graphiti_core/graphiti.py`, verified against the installed 0.29.3 wheel),
and named databases are a Neo4j **Enterprise** feature — Community (what
`docker-compose.yml` runs) only has the one default database. So every call
below uses Graphiti's default group (never passes `group_id`), which keeps
everything in the same database LightRAG's `Neo4JStorage` already writes
into. This is safe: Graphiti's own node labels (`Entity`, `Episodic`, and
the custom types below) don't collide with LightRAG's schema, so "shared
instance, namespaced apart" falls out for free without any extra work.
Multi-repo isolation (Enterprise multi-database, or a hand-rolled namespace
property that doesn't ride `group_id`'s database routing) is future work.

**Providers reuse exactly what `tools/doc_retriever.py` already pays for**:
Anthropic for the LLM calls Graphiti makes internally to extract entities
and edges from an episode (`AnthropicClient()` reads `ANTHROPIC_API_KEY`
itself), Voyage AI for embeddings (`VOYAGE_API_KEY` — Anthropic has no
embeddings endpoint of its own). No new secrets, no third provider.
"""

from __future__ import annotations

import os
from datetime import datetime

# aiohttp (used by voyageai's AsyncClient, which VoyageAIEmbedder below goes
# through) builds its own SSL context from Python's OpenSSL trust store
# rather than falling back to `certifi` the way `requests`/`httpx` do — on a
# python.org-installed Python that store is often empty, so every async
# Voyage embedding call fails with a ClientConnectorCertificateError
# ("unable to get local issuer certificate") until this is set. `src/main.py`
# sets the same thing for the same reason (its own comment already calls out
# that this module needs it too) — set here as well, not just there, so
# `src.memory` works whether or not `src.main` was ever imported first (a
# standalone script per docs/TESTING.md's Tier 8 Step 1, for example).
# `setdefault` — never overrides a value the environment already set.
import certifi  # noqa: E402

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool  # noqa: E402
from graphiti_core import Graphiti  # noqa: E402
from graphiti_core.cross_encoder.client import CrossEncoderClient  # noqa: E402
from graphiti_core.embedder.voyage import VoyageAIEmbedder, VoyageAIEmbedderConfig  # noqa: E402
from graphiti_core.llm_client.anthropic_client import AnthropicClient  # noqa: E402
from graphiti_core.llm_client.config import LLMConfig  # noqa: E402
from graphiti_core.nodes import EpisodeType  # noqa: E402
from loguru import logger  # noqa: E402
from pydantic import BaseModel  # noqa: E402


class PassthroughReranker(CrossEncoderClient):
    """Graphiti's constructor requires *some* cross-encoder, but the basic
    `graphiti.search()` this module calls runs `EDGE_HYBRID_SEARCH_RRF`
    (BM25 + cosine similarity + reciprocal-rank-fusion) — it never actually
    invokes one. Graphiti's own default, `OpenAIRerankerClient`, would both
    force a third paid provider onto this stack *and* fail at construction
    time with no `OPENAI_API_KEY` set (the `openai` SDK raises in its own
    constructor, not lazily) — for a component this integration doesn't
    exercise. This stub just preserves input order with a descending dummy
    score, satisfying the interface. Swap it for a real reranker (e.g.
    `OpenAIRerankerClient` or `GeminiRerankerClient`) if a future pass moves
    to `graphiti.search_()`'s cross-encoder-reranked recipe instead."""

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        n = len(passages)
        return [(passage, (n - i) / n) for i, passage in enumerate(passages)] if n else []


# --- Ontology: generic, core-engine-owned, deliberately not Kyverno-specific ---
#
# These are what Graphiti's own LLM extraction step (Anthropic) classifies
# entities/edges into when processing an episode. Kept generic on purpose —
# nothing here (no `ClusterPolicy`, no Kyverno label names) is
# project-specific; that's a future skill-pack extension seam, same split
# labels/exclusion-lists already have between `skills/kyverno/*.md` and
# core code. See docs/kyctrl_extra_features.md Dimension 3.


class Issue(BaseModel):
    """A GitHub issue filed against the target repository — a bug report,
    feature request, or question."""


class PullRequest(BaseModel):
    """A GitHub pull request — a proposed code change, including
    dependency bumps."""


class Contributor(BaseModel):
    """A person who has opened, commented on, authored, or reviewed an
    issue or pull request."""


class Package(BaseModel):
    """A software dependency (e.g. a Go module, npm package, GitHub Action)
    referenced by a version bump."""


class Regression(BaseModel):
    """A bug or behavior regression traced back to a specific change."""


EntityTypes: dict[str, type[BaseModel]] = {
    "Issue": Issue,
    "PullRequest": PullRequest,
    "Contributor": Contributor,
    "Package": Package,
    "Regression": Regression,
}


class CausedRegression(BaseModel):
    """The source entity (typically a Package bump or PullRequest) caused
    the regression described by the target entity."""


class RelatedTo(BaseModel):
    """The source and target entities are related for a stated reason
    (e.g. a shared label, a shared root cause)."""


EdgeTypes: dict[str, type[BaseModel]] = {
    "CAUSED_REGRESSION": CausedRegression,
    "RELATED_TO": RelatedTo,
}


def build_graphiti_client(uri: str, user: str, password: str) -> Graphiti:
    """Construction only — no network call happens here. `main.py`'s
    startup hook is what actually touches the database
    (`build_indices_and_constraints()`); `runtime.get_memory_client()` is
    what calls this, once, behind an `@lru_cache`."""
    return Graphiti(
        uri=uri,
        user=user,
        password=password,
        # graphiti-core's own AnthropicClient default (no config passed) is
        # DEFAULT_MODEL = "claude-haiku-4-5-latest" — that alias doesn't
        # exist on Anthropic's API (only the dated
        # "claude-haiku-4-5-20251001" does), confirmed live: every
        # write_episode call was silently failing with a 404 not_found_error
        # and falling back to "continuing without memory" until this was
        # set explicitly.
        llm_client=AnthropicClient(LLMConfig(model="claude-haiku-4-5-20251001")),
        embedder=VoyageAIEmbedder(VoyageAIEmbedderConfig(api_key=os.environ.get("VOYAGE_API_KEY"))),
        cross_encoder=PassthroughReranker(),
    )


async def write_episode(
    memory: Graphiti,
    *,
    name: str,
    episode_body: str,
    source_description: str,
    reference_time: datetime,
) -> list[str]:
    """Turns one agent run's outcome into a Graphiti episode. `reference_time`
    is required with no default here, same as `Graphiti.add_episode`'s own
    signature (it has none upstream either) — every call site must pass
    `datetime.now(timezone.utc)` explicitly rather than this function
    silently inventing a time the caller didn't actually observe.

    Never raises: a Neo4j hiccup or a rate-limited provider call here must
    never fail the agent run whose outcome it's trying to remember — same
    "any failure = treated as unset" reasoning as `get_repo_variable`. Logs
    and returns `[]` on any failure, which callers feed straight into
    `AuditEntry.memory_refs` (`None`/`[]` either way means "nothing to
    reference," never a crash).
    """
    try:
        result = await memory.add_episode(
            name=name,
            episode_body=episode_body,
            source_description=source_description,
            reference_time=reference_time,
            source=EpisodeType.text,
            entity_types=EntityTypes,
            edge_types=EdgeTypes,
        )
    except Exception as e:
        logger.warning(f"write_episode({name!r}) failed, continuing without memory: {e}")
        return []
    return [node.uuid for node in result.nodes] + [edge.uuid for edge in result.edges]


async def search_context(memory: Graphiti, *, query: str, limit: int) -> list[str]:
    """The facts (`EntityEdge.fact` strings) most relevant to `query`, via
    Graphiti's basic hybrid search (BM25 + cosine similarity + RRF — see
    `PassthroughReranker` above for why no cross-encoder is involved).
    Same never-raise contract as `write_episode`."""
    try:
        edges = await memory.search(query, num_results=limit)
    except Exception as e:
        logger.warning(f"search_context(query={query!r}) failed, continuing without memory: {e}")
        return []
    return [edge.fact for edge in edges]


def build_memory_tool_server(memory: Graphiti, *, default_limit: int) -> McpSdkServerConfig:
    """The one read-only tool memory exposes to an agent mid-run — same
    shape/error-handling convention as `github_tools.build_issue_tool_server`.
    Writes are never a tool (see this module's docstring and
    `_shared.memory_write`) — only reads are the model's to decide."""

    @tool("search_memory", "Search the agent's temporal memory graph for facts relevant to a query.", {"query": str})
    async def search_memory(args: dict) -> dict:
        facts = await search_context(memory, query=args["query"], limit=default_limit)
        text = "\n".join(f"- {fact}" for fact in facts) if facts else "No relevant memory found."
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server(name="memory-tools", tools=[search_memory])
