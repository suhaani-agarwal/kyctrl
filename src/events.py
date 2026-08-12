"""Event-source abstraction.

`main.py` converts every incoming webhook into an `Event` *before* handing
it to agent code. Agents only ever see `Event`, never raw GitHub JSON. This
is the seam that lets a second event source (Slack, a GitHub Actions cron
ping, GitHub Discussions) be added later as "one more thing that produces
an Event" without touching a single agent — see the "Extension seams"
section of the build plan.

`EVENT_HANDLERS` is a registry, not an if/elif ladder in `main.py`. Adding
a new workflow (proactive risk scoring, a specialized subagent) means
registering one more entry here, not editing the dispatcher.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from pydantic import BaseModel


class Event(BaseModel):
    source: str  # "github" today; "slack" / "cron" later
    type: str  # e.g. "pull_request", "issues", "issue_comment"
    action: str | None = None  # e.g. "opened", "synchronize", "labeled"
    external_id: str  # stable id for dedup/audit linking, e.g. "gh-pr-123"
    payload: dict[str, Any]  # raw source payload, only ever read inside the
    # one adapter that produced this Event — agents work off fields the
    # adapter extracts onto the Event, not off `payload` directly, once an
    # agent needs more than the passthrough fields above.


AgentHandler = Callable[[Event], Awaitable[None]]

EVENT_HANDLERS: dict[str, AgentHandler] = {}


def register_handler(event_type: str) -> Callable[[AgentHandler], AgentHandler]:
    """Decorator agent modules use to register themselves, e.g.:

        @register_handler("pull_request")
        async def handle(event: Event) -> None: ...
    """

    def decorator(fn: AgentHandler) -> AgentHandler:
        EVENT_HANDLERS[event_type] = fn
        return fn

    return decorator
