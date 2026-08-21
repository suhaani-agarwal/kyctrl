"""Graphiti temporal memory — lets an agent recall past runs, not just log
them. `write_episode` turns a run's outcome into a timestamped Graphiti
episode; `search_context` turns a query into the facts most relevant to it.
`src/agents/_shared.py`'s `memory_search`/`memory_write` are what agents
actually call; this module owns construction and the two raw operations.

Shares the same Neo4j instance as `tools/doc_retriever.py`'s LightRAG store
rather than a separate one — named databases are a Neo4j Enterprise-only
feature, and Community (what `docker-compose.yml` runs) has just the one
default database. Every call below uses Graphiti's default group, so its
node labels (`Entity`, `Episodic`, the custom types below) stay separate
from LightRAG's schema without a second database. Multi-repo isolation is
future work.

Providers reuse what `doc_retriever.py` already pays for: Anthropic for
entity/edge extraction, Voyage AI for embeddings. No new secrets.
"""

from __future__ import annotations

import os
from datetime import datetime

# Same certifi workaround as src/main.py, set here too so this module works
# standalone (not just when main.py already ran). `setdefault` — never
# overrides a value the environment already set.
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
    """Graphiti's constructor requires a cross-encoder, but the basic
    `graphiti.search()` this module uses (BM25 + cosine + RRF) never
    actually calls one. The real default, `OpenAIRerankerClient`, would
    force a third paid provider and fail at construction with no
    `OPENAI_API_KEY` set — so this stub just preserves input order with a
    descending dummy score. Swap in a real reranker if a future pass moves
    to Graphiti's cross-encoder-reranked search instead."""

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        n = len(passages)
        return [(passage, (n - i) / n) for i, passage in enumerate(passages)] if n else []


# --- Ontology: generic, core-engine-owned, deliberately not Kyverno-specific ---
#
# What Graphiti's LLM extraction step classifies entities/edges into when
# processing an episode. Kept generic (no Kyverno-specific types) so it
# stays reusable across projects, same as the skill packs.


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
        # graphiti-core's own default model alias ("claude-haiku-4-5-latest")
        # doesn't exist on Anthropic's API — pin the dated model explicitly.
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
    has no default — every call site must pass `datetime.now(timezone.utc)`
    explicitly rather than this function inventing a time.

    Never raises: a Neo4j hiccup or rate-limited provider call must never
    fail the agent run it's trying to remember. Logs and returns `[]` on
    any failure."""
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
