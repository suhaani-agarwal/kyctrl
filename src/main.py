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
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Must run before any `src.*` import that reads os.environ at call time
# (github_auth, runtime) — otherwise .env-only vars are invisible to them.
load_dotenv()

# aiohttp (used by voyageai's AsyncClient, which both src/tools/doc_retriever.py
# and src/memory.py's Graphiti embedder go through) builds its own SSL context
# from Python's OpenSSL trust store rather than falling back to `certifi` the
# way `requests`/`httpx` do — on a python.org-installed Python that store is
# often empty, so every async Voyage embedding call fails with a
# ClientConnectorCertificateError ("unable to get local issuer certificate")
# until this is set. Confirmed live, not a guess — see docs/TESTING.md's Tier
# 5 troubleshooting note. Harmless if the system store is already populated.
import certifi  # noqa: E402

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from loguru import logger  # noqa: E402

from src.app_logging import setup_logging  # noqa: E402
from src.audit import entry_to_dict
from src.events import EVENT_HANDLERS, Event, dispatch
from src.runtime import get_audit_writer, get_client, get_memory_client, get_target_repo
from src.slack_app import get_bolt_app

# Side-effect imports: populate EVENT_HANDLERS. Several modules can (and do)
# register on the same event type — see events.py's fan-out docstring.
import src.agents.dependabot  # noqa: F401,E402
import src.agents.issue_triage  # noqa: F401,E402
import src.agents.coach  # noqa: F401,E402
import src.agents.security_agent  # noqa: F401,E402
import src.agents.pattern_agent  # noqa: F401,E402
import src.agents.reproduction  # noqa: F401,E402
import src.agents.qa_assistant  # noqa: F401,E402
import src.agents.doc_index_refresh  # noqa: F401,E402
from src.tools.github_tools import set_repo_variable  # noqa: E402

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: idempotent schema setup — same role `SQLModel.metadata.create_all`
    # plays in `audit.get_engine`. `get_memory_client()` already returns
    # `None` when `memory.enabled` is `false` (the default) or Neo4j env
    # vars are unset, so this is a no-op in the common case.
    memory = get_memory_client()
    if memory is not None:
        await memory.build_indices_and_constraints()
        logger.info("Graphiti memory: indices/constraints ready")

    yield

    # Shutdown
    if memory is not None:
        await memory.close()


app = FastAPI(title="kyctrl — Kyverno AI Maintainer Assistant", lifespan=lifespan)

_slack_handler = None
_bolt_app = get_bolt_app()
if _bolt_app is not None:
    from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

    _slack_handler = AsyncSlackRequestHandler(_bolt_app)


@app.post("/slack/events")
async def slack_events(request: Request):
    """Mounts the Slack Bolt app (see slack_app.py) at a single route — Bolt
    owns signature verification, the url_verification challenge, and event
    routing from here; this endpoint exists only when Slack credentials are
    configured (see get_bolt_app's docstring)."""
    if _slack_handler is None:
        raise HTTPException(status_code=503, detail="Slack integration not configured (SLACK_BOT_TOKEN/SLACK_SIGNING_SECRET unset)")
    return await _slack_handler.handle(request)

EXTERNAL_ID_FIELD = {
    "pull_request": lambda p: f"gh-pr-{p['pull_request']['number']}",
    "issues": lambda p: f"gh-issue-{p['issue']['number']}",
    "status": lambda p: f"gh-status-{p['sha'][:12]}",
    "discussion": lambda p: f"gh-discussion-{p['discussion']['number']}",
    "discussion_comment": lambda p: f"gh-discussion-comment-{p['comment']['id']}",
    "workflow_run": lambda p: f"gh-workflow-run-{p['workflow_run']['id']}",
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

    if event.type not in EVENT_HANDLERS:
        logger.debug(f"No handler registered for event type {event.type!r}, ignoring")
        return {"status": "ignored", "reason": "no handler for this event type"}

    background_tasks.add_task(dispatch, event)
    return {"status": "accepted"}


@app.post("/internal/cron/{job}")
async def cron_trigger(job: str, request: Request, background_tasks: BackgroundTasks):
    """Ingress for GitHub Actions `schedule:`-triggered jobs (Pattern Agent,
    doc-index refresh) — see `.github/workflows/pattern-agent-cron.yaml`
    (the doc-index-refresh equivalent isn't built yet; add it the same way
    when needed). Not GitHub-HMAC-signed like `/webhook` (these aren't
    GitHub webhook deliveries), so a separate shared-secret header is the
    auth mechanism instead."""
    secret = os.environ.get("CRON_SECRET")
    if not secret or not hmac.compare_digest(request.headers.get("X-Cron-Secret", ""), secret):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Cron-Secret")
    if job not in ("pattern-agent", "doc-index-refresh"):
        raise HTTPException(status_code=404, detail=f"Unknown cron job {job!r}")

    event = Event(source="cron", type=job, external_id=f"cron-{job}-{request.headers.get('X-Cron-Run-Id', 'manual')}", payload={})
    logger.info(f"Received cron trigger: {job}")
    background_tasks.add_task(dispatch, event)
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
