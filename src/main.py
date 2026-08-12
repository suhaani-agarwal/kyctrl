"""FastAPI webhook receiver.

Converts every GitHub webhook into an `Event` (see events.py) and hands it
to whichever handler is registered for that event type — importing the
agent modules below is what populates `EVENT_HANDLERS`, via their
`@register_handler(...)` decorators. Returns 200 immediately and does the
actual agent work in a background task, per GitHub's <10s delivery
requirement.

Also serves the dashboard and its small JSON API (`/api/audit`,
`/api/stats`, `/api/kill-switch`).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

from dotenv import load_dotenv

# Must run before any `src.*` import that reads os.environ at call time
# (github_auth, runtime) — otherwise .env-only vars are invisible to them.
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from loguru import logger  # noqa: E402

from src.app_logging import setup_logging  # noqa: E402
from src.audit import entry_to_dict
from src.events import EVENT_HANDLERS, Event
from src.runtime import get_audit_writer, get_client, get_target_repo

# Side-effect imports: populate EVENT_HANDLERS.
import src.agents.dependabot  # noqa: F401,E402
import src.agents.issue_triage  # noqa: F401,E402
from src.tools.github_tools import set_repo_variable  # noqa: E402

setup_logging()

app = FastAPI(title="kyctrl — Kyverno AI Maintainer Assistant")

EXTERNAL_ID_FIELD = {
    "pull_request": lambda p: f"gh-pr-{p['pull_request']['number']}",
    "issues": lambda p: f"gh-issue-{p['issue']['number']}",
}


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if secret:
        if not verify_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    else:
        logger.warning("GITHUB_WEBHOOK_SECRET not set — accepting webhook unverified (local dev only)")

    event_type = request.headers.get("X-GitHub-Event")
    if not event_type:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Event header")

    payload = await request.json()
    external_id_fn = EXTERNAL_ID_FIELD.get(event_type)
    external_id = external_id_fn(payload) if external_id_fn else f"gh-{event_type}-{payload.get('action', 'unknown')}"

    event = Event(
        source="github",
        type=event_type,
        action=payload.get("action"),
        external_id=external_id,
        payload=payload,
    )
    logger.info(f"Received event: {event.type}.{event.action} ({event.external_id})")

    handler = EVENT_HANDLERS.get(event.type)
    if handler is None:
        logger.debug(f"No handler registered for event type {event.type!r}, ignoring")
        return {"status": "ignored", "reason": "no handler for this event type"}

    background_tasks.add_task(handler, event)
    return {"status": "accepted"}


@app.get("/api/audit")
async def api_audit(limit: int = 50):
    entries = get_audit_writer().recent(limit=limit)
    return JSONResponse([entry_to_dict(e) for e in entries])


@app.get("/api/stats")
async def api_stats(since_days: int = 7):
    return get_audit_writer().stats(since_days=since_days)


@app.get("/api/kill-switch")
async def api_kill_switch_status():
    from src.runtime import get_repo_variable

    value = get_repo_variable("AI_MAINTAINER_ENABLED")
    return {"AI_MAINTAINER_ENABLED": value, "enabled": value is None or value.strip().lower() != "false"}


@app.post("/api/kill-switch")
async def api_kill_switch(enabled: bool):
    """The ONE place the kill switch can be flipped from the dashboard.
    Calls `set_repo_variable` directly — this is never offered to the
    agent as a tool (see github_tools.py's module docstring)."""
    gh = get_client()
    set_repo_variable(gh, get_target_repo(), "AI_MAINTAINER_ENABLED", "true" if enabled else "false")
    return {"AI_MAINTAINER_ENABLED": enabled}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    path = Path(__file__).parent / "dashboard" / "index.html"
    return HTMLResponse(path.read_text())
