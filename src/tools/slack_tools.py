"""Slack actions, split the same way `github_tools.py` is: plain functions
here (used directly by whichever agent needs to post — Security Agent's
private report, Q&A's public/escalation replies), `@tool`-wrapped agent-
facing versions live next to the agent that needs them (see
`qa_assistant.py`), not here — mirrors the split already established for
GitHub tools.

Deliberately a thin `slack_sdk.WebClient`, not the `slack_bolt.App` used by
`slack_app.py` for the events adapter — posting a message doesn't need
Bolt's event-routing machinery, and keeping this module independent of
`slack_app.py` avoids a needless import coupling between "receive Slack
events" and "post a Slack message" (an agent posting a message never needs
to know whether Bolt is even mounted).

Every function here is a no-op (logged, not raised) when `SLACK_BOT_TOKEN`
isn't set — the same "absence of credentials degrades gracefully, is never
a hard crash" posture as the rest of this codebase's optional integrations,
so `workflows.qa_assistant`/`security_agent` can stay off in config while
Slack isn't configured yet, without every code path needing its own guard.
"""

from __future__ import annotations

import os
from functools import lru_cache

from loguru import logger


@lru_cache
def get_slack_client():
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return None
    from slack_sdk import WebClient

    return WebClient(token=token)


def post_message(channel: str, text: str, *, thread_ts: str | None = None) -> bool:
    """Posts `text` to `channel` (a channel name or id). Returns True on
    success, False on any failure (missing token, Slack API error) — never
    raises, since a Slack posting failure should never take down the agent
    run that triggered it (the audit log entry is the durable record either
    way)."""
    client = get_slack_client()
    if client is None:
        logger.warning(f"SLACK_BOT_TOKEN not set — skipping Slack post to {channel!r}")
        return False
    try:
        client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
        return True
    except Exception as e:
        logger.warning(f"Slack post_message to {channel!r} failed: {e}")
        return False
