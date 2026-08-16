"""Event-source abstraction.

`main.py` converts every incoming webhook into an `Event` *before* handing
it to agent code. Agents only ever see `Event`, never raw GitHub JSON. This
is the seam that lets a second event source (Slack, a GitHub Actions cron
ping, GitHub Discussions) be added later as "one more thing that produces
an Event" without touching a single agent.

`EVENT_HANDLERS` is a registry, not an if/elif ladder in `main.py`. Adding a
new workflow means registering one more entry here, not editing the
dispatcher. One event type can fan out to more than one independent agent —
e.g. a `pull_request` event fires both `dependabot.py` (bot PRs) and
`coach.py` (human PRs); an `issues` event fires both `issue_triage.py` and
`security_agent.py`, each internally deciding whether it applies. This is
the mechanical basis for kyctrl_extra_features.md Dimension 2's multi-agent
system: several specialized agents independently reacting to the same
event, not one monolith branching internally.
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
    payload: dict[str, Any]  # raw source payload, only ever read inside the
    # one adapter that produced this Event — agents work off fields the
    # adapter extracts onto the Event, not off `payload` directly, once an
    # agent needs more than the passthrough fields above.


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
    handler's exception is logged and swallowed, never allowed to cancel or
    block its siblings — a bug in, say, `coach.py` must not stop
    `dependabot.py` from processing the same `pull_request` event. Callers
    (webhook/Slack/cron endpoints) fire this as a background task; nothing
    here returns a value for them to inspect."""
    handlers = EVENT_HANDLERS.get(event.type, [])
    if not handlers:
        logger.debug(f"No handler registered for event type {event.type!r}, ignoring")
        return

    results = await asyncio.gather(*(h(event) for h in handlers), return_exceptions=True)
    for handler, result in zip(handlers, results):
        if isinstance(result, Exception):
            logger.error(f"Handler {handler!r} failed on event {event.external_id}: {result}")
