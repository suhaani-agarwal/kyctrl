"""Event-source abstraction.

`main.py` converts every incoming webhook into an `Event` before handing it
to agent code — agents only ever see `Event`, never raw GitHub JSON. This
is the seam that lets other event sources (Slack, a cron ping, GitHub
Discussions) plug in later without touching agent code.

`EVENT_HANDLERS` is a registry, not an if/elif ladder — adding a workflow
means registering one more entry, not editing the dispatcher. One event
type can fan out to multiple independent agents (e.g. `pull_request` fires
both `dependabot.py` and `coach.py`, each deciding internally whether it
applies), which is what lets several specialized agents react to the same
event instead of one monolith branching internally.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from loguru import logger
from pydantic import BaseModel


class Event(BaseModel):
    source: str  # "github" today; "slack" / "cron" now too
    type: str  # e.g. "pull_request", "issues", "issue_comment", "app_mention"
    action: str | None = None  # e.g. "opened", "synchronize", "labeled"
    external_id: str  # stable id for dedup/audit linking, e.g. "gh-pr-123"
    # Raw source payload — only read inside the adapter that produced this
    # Event. Agents work off the fields above, not `payload` directly.
    payload: dict[str, Any]


AgentHandler = Callable[[Event], Awaitable[None]]

EVENT_HANDLERS: dict[str, list[AgentHandler]] = {}


def register_handler(event_type: str) -> Callable[[AgentHandler], AgentHandler]:
    """Decorator agent modules use to register themselves, e.g.:

        @register_handler("pull_request")
        async def handle(event: Event) -> None: ...

    Multiple handlers may register for the same event type — each runs
    independently (see `dispatch`), so one agent module never needs to know
    about another's existence to coexist on the same event.
    """

    def decorator(fn: AgentHandler) -> AgentHandler:
        EVENT_HANDLERS.setdefault(event_type, []).append(fn)
        return fn

    return decorator


async def dispatch(event: Event) -> None:
    """Runs every handler registered for `event.type` concurrently. One
    handler's exception is logged and swallowed, never allowed to block its
    siblings — a bug in one agent must not stop another from processing
    the same event."""
    handlers = EVENT_HANDLERS.get(event.type, [])
    if not handlers:
        logger.debug(f"No handler registered for event type {event.type!r}, ignoring")
        return

    results = await asyncio.gather(*(h(event) for h in handlers), return_exceptions=True)
    for handler, result in zip(handlers, results):
        if isinstance(result, Exception):
            logger.error(f"Handler {handler!r} failed on event {event.external_id}: {result}")
