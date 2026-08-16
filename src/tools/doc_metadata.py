"""`source_url -> kyverno_version` side table.

LightRAG's structured retrieval API (`aquery_data`) returns `file_path` per
chunk but has no per-chunk custom-metadata field to carry an arbitrary tag
like a Kyverno version alongside it. Rather than rely on unverified library
support for something this load-bearing, the version tag is kept here, in a
tiny local SQLite table keyed by the same `source_url` LightRAG already
tracks as `file_path` — the same reasoning as the citation guardrail in
`qa_assistant.py`: a correctness property worth a few lines of plain Python
rather than a dependency on how thoroughly a third-party library passes
metadata through.

Deliberately a separate, tiny SQLite file from `audit.sqlite3` — this is
doc-index metadata, not an audit trail, and gets rebuilt/refreshed by
`scripts/build_doc_index.py`, not appended-to like the audit log.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS doc_metadata ("
        "source_url TEXT PRIMARY KEY, "
        "kyverno_version TEXT NOT NULL, "
        "title TEXT"
        ")"
    )
    conn.commit()
    return conn


def upsert(db_path: str | Path, source_url: str, kyverno_version: str, title: str | None = None) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO doc_metadata (source_url, kyverno_version, title) VALUES (?, ?, ?) "
            "ON CONFLICT(source_url) DO UPDATE SET kyverno_version = excluded.kyverno_version, title = excluded.title",
            (source_url, kyverno_version, title),
        )
        conn.commit()
    finally:
        conn.close()


def get_version(db_path: str | Path, source_url: str) -> str | None:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT kyverno_version FROM doc_metadata WHERE source_url = ?", (source_url,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
