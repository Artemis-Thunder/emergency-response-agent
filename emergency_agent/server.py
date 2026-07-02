# ruff: noqa: E501
"""Ambient event-driven web service for the Emergency Response Agent.

Accepts event-trigger messages via POST and feeds each into the ADK 2.0
Workflow graph.  Designed to sit behind a Pub/Sub push subscription or
any HTTP event source.

Run:
    make serve          # shortcut
    uv run uvicorn emergency_agent.server:fastapi_app --host 0.0.0.0 --port 8080

POST /event
    Body (Pub/Sub push envelope):
    {
      "message": {
        "data": "<base64-encoded-json>",
        "messageId": "12345",
        "publishTime": "2026-07-02T12:00:00Z"
      },
      "subscription": "projects/my-project/subscriptions/emergency-reports-sub"
    }

    Or plain JSON (local testing):
    {
      "data": { "report_id": "ER-2026-000001", ... }
    }
"""

import json
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from .agent import app as adk_app

# ---------------------------------------------------------------------------
# Runner — single instance shared across requests
# ---------------------------------------------------------------------------
runner: InMemoryRunner | None = None

# Track which user_id owns each session so /approve can resolve it
# without the caller needing to know the original source.
_session_owners: dict[str, str] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialise the InMemoryRunner on startup."""
    global runner
    runner = InMemoryRunner(app=adk_app)
    yield
    runner = None


fastapi_app = FastAPI(
    title="Emergency Response Agent — Ambient Service",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Subscription path normalizer
# ---------------------------------------------------------------------------
_SUBSCRIPTION_RE = re.compile(
    r"^projects/[^/]+/subscriptions/(.+)$"
)


def _normalize_subscription(raw: str) -> str:
    """Strip the fully-qualified Pub/Sub subscription path to a short name.

    'projects/my-project/subscriptions/emergency-reports-sub'
        → 'emergency-reports-sub'

    If it's already short or doesn't match, return as-is.
    """
    m = _SUBSCRIPTION_RE.match(raw)
    return m.group(1) if m else raw


# ---------------------------------------------------------------------------
# POST /event — the ambient entry point
# ---------------------------------------------------------------------------
@fastapi_app.post("/event")
async def handle_event(request: Request):
    """Accept an event-trigger message and feed it into the workflow.

    Supports two envelope formats:
      1. Pub/Sub push: { "message": { "data": "...", ... }, "subscription": "..." }
      2. Plain JSON:   { "data": { ... } }
    """
    body = await request.json()

    # --- Determine the source subscription (for readable session naming) ---
    subscription = body.get("subscription", "local")
    source = _normalize_subscription(subscription)

    # --- Extract the inner payload ---
    if "message" in body:
        # Pub/Sub push envelope — data is base64 inside message
        inner = body["message"]
        payload = {"data": inner.get("data", {})}
    else:
        # Plain JSON — pass through as-is
        payload = body

    # --- Derive a session ID from the report_id if possible ---
    # Peek into the data to extract report_id for session naming
    data = payload.get("data", {})
    if isinstance(data, str):
        # base64 — don't decode here, let parse_event handle it
        session_id = f"{source}-unknown"
    elif isinstance(data, dict):
        report_id = data.get("report_id", "unknown")
        session_id = f"{source}-{report_id}"
    else:
        session_id = f"{source}-unknown"

    # --- Create a fresh session per event ---
    session = await runner.session_service.create_session(
        app_name="emergency_agent",
        user_id=source,
    )
    # Remember which user_id owns this session for /approve lookups
    _session_owners[session.id] = source

    # --- Run the workflow ---
    events_log = []
    needs_approval = False
    async for event in runner.run_async(
        user_id=source,
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part.from_text(text=json.dumps(payload))],
        ),
    ):
        if event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    events_log.append(part.text)
                elif hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    interrupt_id = fc.args.get("interruptId", "unknown")
                    events_log.append(
                        f"[HITL] Workflow paused — interrupt_id={interrupt_id}"
                    )
        # ADK 2.0 signals HITL via long_running_tool_ids
        if hasattr(event, "long_running_tool_ids") and event.long_running_tool_ids:
            needs_approval = True

    return JSONResponse(
        content={
            "session_id": session.id,
            "user_id": source,
            "source": source,
            "events": events_log,
            "needs_human_approval": needs_approval,
            "status": "pending_approval" if needs_approval else "completed",
        },
        status_code=202 if needs_approval else 200,
    )


# ---------------------------------------------------------------------------
# POST /approve — human dispatcher approval endpoint
# ---------------------------------------------------------------------------
@fastapi_app.post("/approve")
async def approve_dispatch(request: Request):
    """Resume a paused workflow with human dispatcher decision.

    Body:
    {
      "session_id": "...",
      "user_id": "...",
      "approved": true/false
    }
    """
    from google.adk.workflow.utils._workflow_hitl_utils import create_request_input_response

    body = await request.json()
    session_id = body["session_id"]
    # Resolve user_id from server-side tracking (caller can override)
    user_id = body.get("user_id") or _session_owners.get(session_id, "dispatcher")
    approved = body.get("approved", False)

    resume_part = create_request_input_response(
        interrupt_id="dispatch_approval",
        response={"approved": approved},
    )

    events_log = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[resume_part]),
    ):
        if event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    events_log.append(part.text)

    return JSONResponse(content={
        "session_id": session_id,
        "decision": "approved" if approved else "declined",
        "events": events_log,
    })


# ---------------------------------------------------------------------------
# GET /health — basic liveness check
# ---------------------------------------------------------------------------
@fastapi_app.get("/health")
async def health():
    return {"status": "ok", "runner": runner is not None}
