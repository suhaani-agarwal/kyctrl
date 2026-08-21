"""Retrieval layer for the Q&A assistant, backed by LightRAG
(`graph_storage="Neo4JStorage"` — the same Neo4j instance
`docker-compose.yml`'s `neo4j` service provides).

Scope note: LightRAG's Neo4j integration covers graph storage
(entities/relationships) only — the embedding index it uses for hybrid
retrieval still lives in a local file under `working_dir`
(`NanoVectorDBStorage`, LightRAG's default).

`search_docs()` uses `LightRAG.aquery_data()` (structured retrieval, no LLM
generation) rather than `aquery()` — answer composition and citation
enforcement happen in `qa_assistant.py`'s tool guardrail, never silently
inside a retrieval call.

Embeddings go through Voyage AI; Anthropic has no embeddings endpoint of
its own. LightRAG's own LLM calls (entity extraction during indexing) go
through Claude.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from lightrag import LightRAG, QueryParam
from lightrag.kg.shared_storage import initialize_pipeline_status
from lightrag.llm.anthropic import anthropic_complete
from lightrag.llm.voyageai import voyageai_embed
from loguru import logger

from src.tools import doc_metadata

WORKING_DIR = os.environ.get("DOC_INDEX_WORKING_DIR", "data/doc_index")
DOC_METADATA_DB = os.environ.get("DOC_METADATA_DB_PATH", "data/doc_metadata.sqlite3")
# Cheap model for indexing-time entity extraction — this runs once per
# crawled document, not per question, so cost/latency matter more than
# for the interactive Q&A path (which uses the agent's own configured
# model via the Claude Agent SDK, not this).
_INDEX_LLM_MODEL = os.environ.get("DOC_INDEX_LLM_MODEL", "claude-haiku-4-5-20251001")

_rag: LightRAG | None = None
# LightRAG's own `file_path` field collapses a URL to just its basename, so
# it can't be trusted as the real source URL. The URL survives intact as
# the prefix of `chunk_id` (`f"{doc_id}-chunk-{order:03d}"`, built from the
# `ids=[source_url]` passed at index time), so this regex recovers it instead.
_CHUNK_ID_SUFFIX_RE = re.compile(r"-chunk-\d{3}$")


def _source_url_from_chunk_id(chunk_id: str) -> str:
    return _CHUNK_ID_SUFFIX_RE.sub("", chunk_id)


@dataclass
class Chunk:
    text: str
    source_url: str
    kyverno_version: str


async def get_rag() -> LightRAG:
    """Module-level singleton, not `@lru_cache` — `LightRAG.initialize_storages()`
    is itself async, and `lru_cache` on an async function caches the
    coroutine object rather than its resolved value (a second `await` on an
    already-awaited coroutine raises), so the cache has to sit around the
    resolved instance instead, guarded by the check below."""
    global _rag
    if _rag is not None:
        return _rag

    os.makedirs(WORKING_DIR, exist_ok=True)
    rag = LightRAG(
        working_dir=WORKING_DIR,
        graph_storage="Neo4JStorage",
        llm_model_func=anthropic_complete,
        llm_model_name=_INDEX_LLM_MODEL,
        embedding_func=voyageai_embed,
    )
    await rag.initialize_storages()
    await initialize_pipeline_status()
    _rag = rag
    return _rag


async def search_docs(query: str, top_k: int = 5, target_version: str | None = None) -> list[Chunk]:
    """The retrieval layer the Q&A agent's `search_docs` tool calls. Ranking
    is LightRAG's own algorithm — a fixed formula, not an LLM judgment call —
    currently `mode="naive"` (plain vector chunk retrieval, ~1 embedding call
    per search_docs call). This was `mode="mix"` (vector + knowledge-graph
    entity/relationship traversal), which is a genuinely better ranking but
    issues several separate Voyage embedding calls per single search_docs
    call (query, entity vectors, relationship vectors). That's fine against
    Voyage's standard rate limits; against the *reduced* 3-RPM/10K-TPM limits
    Voyage applies to accounts with no payment method on file, a single
    question can exceed the entire per-minute budget by itself, and every
    call past it fails outright rather than degrading gracefully. `naive`
    trades away the graph-hybrid ranking to fit inside that cap. Switch back
    to `mode="mix"` once the account has a payment method on file (still
    covered by Voyage's free-token tier — this isn't about spend).

    When `target_version` is given, this re-ranks/filters to prefer chunks
    tagged with that Kyverno version in the `doc_metadata` side table
    (see that module's docstring for why version tagging isn't done through
    LightRAG's own metadata). Falls back to the unfiltered ranking if
    nothing matches that version, so a version-specific question never
    silently comes back empty — this filtering step is deliberately outside
    LightRAG, same reasoning as the citation guardrail in `qa_assistant.py`.
    """
    rag = await get_rag()
    try:
        result = await rag.aquery_data(query, param=QueryParam(mode="naive", top_k=top_k, chunk_top_k=top_k))
    except Exception as e:
        # A rate-limited/unavailable embedding provider must degrade to "no
        # results," never crash the calling agent run — same "any failure =
        # treated as unset" contract src/memory.py already uses for Graphiti.
        # Without this, one failed call here surfaces as a tool error to the
        # model, which — per its own instructions to try different phrasings
        # before giving up — just retries with a new query, each attempt
        # racing the same exhausted rate limit, until max_turns is reached
        # with no answer and no clean escalation either (confirmed live).
        logger.warning(f"search_docs(query={query!r}) failed, returning no results: {e}")
        return []

    if result.get("status") != "success":
        logger.warning(f"search_docs: LightRAG query did not succeed: {result.get('message')}")
        return []

    raw_chunks = result.get("data", {}).get("chunks", [])
    chunks = []
    for c in raw_chunks:
        source_url = _source_url_from_chunk_id(c["chunk_id"])
        chunks.append(
            Chunk(
                text=c["content"],
                source_url=source_url,
                kyverno_version=doc_metadata.get_version(DOC_METADATA_DB, source_url) or "unversioned",
            )
        )

    if target_version:
        version_matched = [c for c in chunks if c.kyverno_version == target_version]
        if version_matched:
            return version_matched[:top_k]

    return chunks[:top_k]
