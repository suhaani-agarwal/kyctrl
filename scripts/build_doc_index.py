#!/usr/bin/env python3
"""Indexes `data/kyverno_docs/*.md` (produced by `scripts/crawl_docs.py`)
into LightRAG — one step, not three (see the plan this replaced Microsoft
GraphRAG's separate index/import scripts with). Because LightRAG dedups and
updates by content hash internally, re-running this against a refreshed
crawl only touches documents that actually changed — this is what makes the
`doc-index-refresh` cron job (see main.py's `/internal/cron/doc-index-refresh`)
cheap: no full rebuild, no re-embedding unchanged pages.

Requires a running Neo4j instance (`docker compose up neo4j`) and
`VOYAGE_API_KEY`/`ANTHROPIC_API_KEY` set — see `tools/doc_retriever.py`'s
docstring for why each is used for what.

Usage:
    python scripts/build_doc_index.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.tools import doc_metadata  # noqa: E402
from src.tools.doc_retriever import DOC_METADATA_DB, get_rag  # noqa: E402

DOCS_DIR = Path("data/kyverno_docs")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n\n(.*)$", re.S)


def _parse_doc(path: Path) -> tuple[str, str, str, str] | None:
    """Returns (source_url, title, kyverno_version, body) or None if the
    file doesn't have the frontmatter block `crawl_docs.py` always writes —
    skipped rather than crashing the whole indexing run over one bad file."""
    match = _FRONTMATTER_RE.match(path.read_text())
    if not match:
        logger.warning(f"{path}: missing frontmatter block, skipping")
        return None
    frontmatter_text, body = match.groups()
    fields = dict(line.split(": ", 1) for line in frontmatter_text.splitlines() if ": " in line)
    try:
        return fields["source_url"], fields.get("title", fields["source_url"]), fields["kyverno_version"], body
    except KeyError as e:
        logger.warning(f"{path}: frontmatter missing required field {e}, skipping")
        return None


async def build_index() -> int:
    if not DOCS_DIR.exists():
        raise SystemExit(f"{DOCS_DIR} does not exist — run scripts/crawl_docs.py first")

    rag = await get_rag()
    indexed = 0
    for path in sorted(DOCS_DIR.glob("*.md")):
        parsed = _parse_doc(path)
        if parsed is None:
            continue
        source_url, title, kyverno_version, body = parsed

        await rag.ainsert(body, ids=[source_url], file_paths=[source_url])
        doc_metadata.upsert(DOC_METADATA_DB, source_url, kyverno_version, title)
        indexed += 1
        logger.info(f"indexed {source_url} (version={kyverno_version})")

    return indexed


if __name__ == "__main__":
    n = asyncio.run(build_index())
    logger.info(f"Indexed {n} document(s) into LightRAG (Neo4j graph storage) + doc_metadata.")
