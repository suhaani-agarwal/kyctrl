#!/usr/bin/env python3
"""Crawls kyverno.io + resolved `question`-labeled GitHub issues into
`data/kyverno_docs/*.md`, one file per document, each with a frontmatter
block (`source_url`, `kyverno_version`, `title`) — the corpus
`scripts/build_doc_index.py` then indexes.

Defaults to plain `httpx` + `markdownify` — no browser. kyverno.io is
server-rendered static HTML (an Astro/Starlight site as of this writing), so
this covers the vast majority of pages without paying Playwright/Chromium's
cost. `--js-render` is documented, not installed by default: for the rare
page that turns out to need JS rendering, `pip install crawl4ai &&
playwright install chromium` first, then pass `--js-render <url>` to fetch
just that page through Crawl4AI instead. Test the default path first —
most of kyverno.io does not need this.

Page discovery is a same-host `/docs/`-scoped link crawl from one seed page
(`DOCS_SEED`), not a sitemap fetch — kyverno.io does not serve
`/sitemap.xml` (confirmed live, not assumed).

Usage:
    python scripts/crawl_docs.py                    # full crawl
    python scripts/crawl_docs.py --limit 20          # cap pages, for a quick manual check
    python scripts/crawl_docs.py --js-render <url>   # one page, via optional Crawl4AI
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import httpx
from loguru import logger
from markdownify import markdownify

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.runtime import get_client, get_target_repo  # noqa: E402

DOCS_SEED = "https://kyverno.io/docs/introduction/"
# kyverno.io moved to an Astro/Starlight site at some point after this script
# was first written, and dropped /sitemap.xml along with it (confirmed via a
# live 404 — not a guess). Starlight's own nav is the only reliable page
# list left, so discovery is a same-host, /docs/-scoped link crawl from one
# seed page instead of a sitemap fetch. Capped at _MAX_DISCOVERY even when
# `limit` is None, so an unbounded run can't wander indefinitely if the site
# structure changes again.
_MAX_DISCOVERY = 500
OUTPUT_DIR = Path("data/kyverno_docs")
_VERSION_IN_PATH_RE = re.compile(r"/v?(\d+\.\d+(?:\.\d+)?)/")
_DOCS_LINK_RE = re.compile(r'href="(/docs/[a-zA-Z0-9_\-/]*)"')


def _slugify(source_url: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", source_url).strip("-")[:150]


def _write_doc(source_url: str, title: str, kyverno_version: str, body_markdown: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{_slugify(source_url)}.md"
    frontmatter = f"---\nsource_url: {source_url}\nkyverno_version: {kyverno_version}\ntitle: {title}\n---\n\n"
    path.write_text(frontmatter + body_markdown)
    logger.info(f"wrote {path} ({len(body_markdown)} chars, version={kyverno_version})")


def _discover_doc_urls(client: httpx.Client, limit: int | None) -> list[str]:
    """BFS over `/docs/` pages starting from `DOCS_SEED`, following only
    same-host, same-prefix links found in each page's HTML. Each fetched
    page is cached in `pages` and reused below so `crawl_docs_site` doesn't
    fetch every URL twice (once to discover links, once to extract content)."""
    cap = limit if limit else _MAX_DISCOVERY
    seen = {DOCS_SEED}
    queue = [DOCS_SEED]
    ordered: list[str] = []
    pages: dict[str, str] = {}

    while queue and len(ordered) < cap:
        url = queue.pop(0)
        try:
            resp = client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning(f"failed to fetch {url} during discovery: {e}")
            continue

        pages[url] = resp.text
        ordered.append(url)
        for path in _DOCS_LINK_RE.findall(resp.text):
            full = f"https://kyverno.io{path}"
            if not full.endswith("/"):
                full += "/"
            if full not in seen:
                seen.add(full)
                queue.append(full)

    if len(ordered) >= _MAX_DISCOVERY and not limit:
        logger.warning(f"doc discovery hit the {_MAX_DISCOVERY}-page cap — some pages may be missing")
    return ordered, pages


def crawl_docs_site(limit: int | None = None) -> int:
    written = 0
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        urls, pages = _discover_doc_urls(client, limit)
        for url in urls:
            html = pages[url]

            title_match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
            title = title_match.group(1).strip() if title_match else url
            body_match = re.search(r"<main[^>]*>(.*?)</main>", html, re.I | re.S)
            html_body = body_match.group(1) if body_match else html
            body_markdown = markdownify(html_body, heading_style="ATX").strip()
            if len(body_markdown) < 40:
                logger.warning(f"{url}: suspiciously little content ({len(body_markdown)} chars) — "
                                f"may need --js-render instead of the default httpx path")
                continue

            version_match = _VERSION_IN_PATH_RE.search(url)
            kyverno_version = version_match.group(1) if version_match else "unversioned"
            _write_doc(url, title, kyverno_version, body_markdown)
            written += 1
    return written


def crawl_resolved_question_issues(limit: int | None = None) -> int:
    """Pulls closed `question`-labeled issues from the target repo as
    additional Q&A corpus documents — real, previously-answered questions
    are some of the highest-value grounding for this bot."""
    gh = get_client()
    repo_full_name = get_target_repo()
    query = f"repo:{repo_full_name} is:issue is:closed label:question"
    results = gh.search_issues(query=query)

    written = 0
    for issue in results:
        if limit and written >= limit:
            break
        comments = list(issue.get_comments())
        answer = comments[0].body if comments else ""
        body_markdown = f"# {issue.title}\n\n{issue.body or ''}\n\n## Answer\n\n{answer}"

        version_match = re.search(r"###\s*Kyverno Version\s*\n+\s*([^\n]+)", issue.body or "", re.I)
        kyverno_version = version_match.group(1).strip() if version_match else "unversioned"

        _write_doc(issue.html_url, issue.title, kyverno_version, body_markdown)
        written += 1
    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of pages/issues crawled (per source)")
    parser.add_argument("--js-render", metavar="URL", help="Fetch a single URL via the optional Crawl4AI path instead of the default crawl")
    args = parser.parse_args()

    if args.js_render:
        try:
            import asyncio

            from crawl4ai import AsyncWebCrawler
        except ImportError:
            raise SystemExit(
                "Crawl4AI is not installed — this is intentional (see this script's docstring). "
                "Run: pip install crawl4ai && playwright install chromium"
            )

        async def _render_one(url: str) -> None:
            async with AsyncWebCrawler() as crawler:
                result = await crawler.arun(url=url)
                version_match = _VERSION_IN_PATH_RE.search(url)
                _write_doc(url, url, version_match.group(1) if version_match else "unversioned", result.markdown)

        asyncio.run(_render_one(args.js_render))
        raise SystemExit(0)

    n_docs = crawl_docs_site(limit=args.limit)
    n_issues = crawl_resolved_question_issues(limit=args.limit)
    logger.info(f"Crawled {n_docs} doc page(s) and {n_issues} resolved question issue(s) into {OUTPUT_DIR}/")
