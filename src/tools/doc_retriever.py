"""Deterministic-ish retrieval layer for the Q&A assistant, backed by
LightRAG (`graph_storage="Neo4JStorage"` — the same Neo4j instance as
`src/graph.py`, `docker-compose.yml`'s `neo4j` service).

Honest scope note: LightRAG's Neo4j integration covers *graph* storage
(entities/relationships) only — as of lightrag-hku 1.5.6 there is no
Neo4j-backed vector store, so the embedding index LightRAG uses for hybrid
retrieval still lives in a local file under `working_dir`
(`NanoVectorDBStorage`, LightRAG's default). The entity/relationship graph
itself — the part other systems (a future tree-sitter code graph, Graphiti
memory) could eventually join against — is genuinely in the shared Neo4j
instance; the vector index is not. Documented here rather than implied by
the module name.

`search_docs()` uses `LightRAG.aquery_data()` (structured retrieval,
*no* LLM generation) rather than `aquery()` (which would have LightRAG
compose its own answer) — the whole point is that answer composition and
citation enforcement happen in `qa_assistant.py`'s tool guardrail, in plain
Python, never silently inside a retrieval call.

Embeddings go through Voyage AI (`lightrag.llm.voyageai.voyageai_embed`) —
Anthropic has no embeddings endpoint of its own; Voyage is Anthropic's own
recommended embedding partner and a first-class lightrag-hku provider.
LightRAG's own LLM calls (entity extraction during indexing) go through
Claude (`lightrag.llm.anthropic.anthropic_complete`).
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
# LightRAG's own `file_path` field is *not* the real URL, despite the
# `file_paths=[source_url]` we pass to `ainsert()` — confirmed by reading
# `lightrag.utils_pipeline.normalize_document_file_path`, which explicitly
# stores only "the canonical basename" of whatever's passed (it's built for
# real filesystem paths, e.g. "install.md", not URLs). `https://.../install/`
# collapses to `"install"` there, which would silently produce fake,
# non-clickable citations if used directly — exactly the failure mode
# `qa_assistant.py`'s citation guardrail exists to prevent.
# The real URL survives intact as the prefix of `chunk_id`, which LightRAG
# builds as `f"{doc_id}-chunk-{order:03d}"` (`utils_pipeline.py`) from the
# `ids=[source_url]` we pass — this is the stable, load-bearing identifier
# `build_doc_index.py` actually controls, so this regex recovers the real
# source_url rather than trusting `file_path`.
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
    is LightRAG's own hybrid (`mode="mix"`: vector-retrieved chunks plus
    graph traversal) — a fixed algorithm, not an LLM judgment call.

    When `target_version` is given, this re-ranks/filters to prefer chunks
    tagged with that Kyverno version in the `doc_metadata` side table
    (see that module's docstring for why version tagging isn't done through
    LightRAG's own metadata). Falls back to the unfiltered ranking if
    nothing matches that version, so a version-specific question never
    silently comes back empty — this filtering step is deliberately outside
    LightRAG, same reasoning as the citation guardrail in `qa_assistant.py`.
    """
    rag = await get_rag()
    result = await rag.aquery_data(query, param=QueryParam(mode="mix", top_k=top_k, chunk_top_k=top_k))

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
