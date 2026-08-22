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

import asyncio
import os
from functools import lru_cache

from loguru import logger

from src.events import Event, dispatch

# AsyncSlackRequestHandler.handle() (see main.py's /slack/events route)
# awaits the *entire* Bolt listener chain before returning the HTTP response
# to Slack — confirmed by reading its source, not assumed. Slack's Events
# API expects that response within 3 seconds or it retries the same event
# delivery up to 3 times, each retry re-triggering this same listener. A
# full Q&A agent turn (several tool calls, several Voyage/Anthropic round
# trips) routinely takes far longer than that, so `await dispatch(...)`
# directly here was silently causing 2-3 duplicate full agent runs per
# question — the real cause of the Voyage rate-limit storms and inflated
# cost seen in testing, not "one question is expensive." Firing it via
# `asyncio.create_task` instead lets this listener return immediately after
# `ack()`, exactly mirroring how main.py's GitHub webhook path already uses
# `background_tasks.add_task(dispatch, event)` for the same reason.


async def _dispatch_in_background(event: Event) -> None:
    try:
        await dispatch(event)
    except Exception:
        logger.exception(f"Unhandled exception dispatching Slack event {event.type!r} ({event.external_id})")

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
        asyncio.create_task(
            _dispatch_in_background(
                Event(
                    source="slack",
                    type="app_mention",
                    external_id=f"slack-{channel}-{thread_ts}",
                    payload=event,
                )
            )
        )

    # Slack sends a plain `message` event alongside `app_mention` for the
    # same channel message (and for every other message the bot can see,
    # e.g. in a DM). We only ever act on `app_mention`/the Assistant thread
    # listeners below, so this is a deliberate no-op — without it, Bolt logs
    # its "unhandled event, here's a listener you could add" suggestion for
    # every single message, which reads like an error but isn't one.
    @app.event("message")
    async def handle_message_event(ack) -> None:
        await ack()

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
        asyncio.create_task(
            _dispatch_in_background(
                Event(
                    source="slack",
                    type="app_mention",
                    external_id=f"slack-assistant-{channel}-{thread_ts}",
                    payload={"text": text, "channel": channel, "thread_ts": thread_ts},
                )
            )
        )

    app.use(assistant)
    return app
