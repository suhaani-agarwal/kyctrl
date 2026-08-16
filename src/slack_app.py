"""Slack Bolt app — events adapter + native AI Assistant surface. Uses
`slack_bolt`'s **async** variant (`AsyncApp`/`AsyncAssistant`) since the rest
of this codebase is async throughout (FastAPI, the Claude Agent SDK's
`query()`, `events.dispatch`) — mixing in the sync `App` would mean either
blocking the event loop or juggling a thread pool for no benefit.

Bolt owns signature verification, the `url_verification` challenge, event
routing, and the 3-second-ack requirement itself — none of that is
hand-rolled here, unlike GitHub's webhook verification in `main.py` (GitHub
gives no SDK for this; Slack does, and Slack's own docs say to use it).
Bolt does **not** replace FastAPI: it's mounted at exactly one route
(`POST /slack/events` in `main.py`) via `AsyncSlackRequestHandler`, and every
listener below just converts a Slack event into an `Event` and calls the
same `events.dispatch()` every other event source uses — so a Slack
question goes through the identical registry, fan-out, and audit trail as
a GitHub webhook.

Returns `None` (not an app) when `SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET`
aren't set, so `main.py` can mount nothing and the rest of the app is
unaffected — same "absence of credentials degrades gracefully" posture as
`tools/slack_tools.py`.
"""

from __future__ import annotations

import os
from functools import lru_cache

from loguru import logger

from src.events import Event, dispatch

_SUGGESTED_PROMPTS = [
    "Does Kyverno support mutate policies for Ingress resources?",
    "How do I verify container image signatures with Kyverno?",
    "What's the difference between a ClusterPolicy and a Policy?",
]


@lru_cache
def get_bolt_app():
    token = os.environ.get("SLACK_BOT_TOKEN")
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
    if not token or not signing_secret:
        logger.warning("SLACK_BOT_TOKEN/SLACK_SIGNING_SECRET not set — Slack adapter not mounted")
        return None

    from slack_bolt.async_app import AsyncApp, AsyncAssistant

    app = AsyncApp(token=token, signing_secret=signing_secret)

    @app.event("app_mention")
    async def handle_app_mention_event(event: dict, ack) -> None:
        await ack()
        channel = event.get("channel")
        thread_ts = event.get("thread_ts") or event.get("ts")
        if not channel or not thread_ts:
            logger.warning("app_mention event missing channel/ts, skipping")
            return
        await dispatch(
            Event(
                source="slack",
                type="app_mention",
                external_id=f"slack-{channel}-{thread_ts}",
                payload=event,
            )
        )

    # Slack's native AI Assistant surface — shows up in the AI side panel
    # rather than as a bot posting in an ordinary channel thread.
    assistant = AsyncAssistant()

    @assistant.thread_started
    async def start_assistant_thread(say, set_suggested_prompts) -> None:
        await set_suggested_prompts(prompts=_SUGGESTED_PROMPTS)

    @assistant.user_message
    async def handle_assistant_message(payload: dict, say) -> None:
        channel = payload.get("channel_id")
        thread_ts = payload.get("thread_ts")
        text = payload.get("text", "")
        if not channel or not thread_ts:
            logger.warning("assistant user_message missing channel_id/thread_ts, skipping")
            return
        await dispatch(
            Event(
                source="slack",
                type="app_mention",
                external_id=f"slack-assistant-{channel}-{thread_ts}",
                payload={"text": text, "channel": channel, "thread_ts": thread_ts},
            )
        )

    app.use(assistant)
    return app
