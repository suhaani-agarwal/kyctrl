"""GitHub Discussions has no REST API — comments/replies only exist via
GraphQL. Raw `httpx.post` against the GraphQL endpoint (httpx is already a
dependency; no new GraphQL client library needed for two mutations).
"""

from __future__ import annotations

import httpx
from loguru import logger

_GRAPHQL_URL = "https://api.github.com/graphql"

_ADD_COMMENT_MUTATION = """
mutation($discussionId: ID!, $body: String!) {
  addDiscussionComment(input: {discussionId: $discussionId, body: $body}) {
    comment { id url }
  }
}
"""


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def add_discussion_comment(discussion_node_id: str, body: str, *, token: str) -> bool:
    """Posts `body` as a top-level comment on the discussion identified by
    its GraphQL node id (not its number — Discussions are addressed by
    node id in the GraphQL schema). `token` comes from
    `runtime.get_github_token()` — this module never reads env vars
    directly, so it works identically under GitHub App auth or local-dev
    PAT auth (see `tools/github_auth.py`). Returns False (never raises) on
    any failure, same "don't crash the triggering agent run over a posting
    failure" posture as `github_tools.py`'s `_tool_error`."""
    try:
        resp = httpx.post(
            _GRAPHQL_URL,
            headers=_headers(token),
            json={"query": _ADD_COMMENT_MUTATION, "variables": {"discussionId": discussion_node_id, "body": body}},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            logger.warning(f"add_discussion_comment GraphQL errors: {data['errors']}")
            return False
        return True
    except httpx.HTTPError as e:
        logger.warning(f"add_discussion_comment failed: {e}")
        return False
