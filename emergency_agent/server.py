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
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
import httpx
from google.adk.runners import InMemoryRunner
from google.genai import types

from .agent import app as adk_app

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runner — single instance shared across requests
# ---------------------------------------------------------------------------
runner: InMemoryRunner | None = None

# Track which user_id owns each session so /approve can resolve it
# without the caller needing to know the original source.
_session_owners: dict[str, str] = {}

# In-memory history for GET /history
_event_history: list[dict] = []


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
    data = payload.get("data", payload)
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
    logger.info("Event received — session=%s source=%s", session.id, source)

    # --- Initialize history entry ---
    import base64
    desc = "Unknown"
    if isinstance(data, dict):
        desc = data.get("description", "Unknown")
    elif isinstance(data, str):
        try:
            decoded = json.loads(base64.b64decode(data).decode("utf-8"))
            desc = decoded.get("description", "Unknown")
        except Exception:
            pass

    history_entry = {
        "session_id": session.id,
        "description": desc,
        "severity_score": None,
        "routing_decision": "unknown",
        "hitl_triggered": False,
        "final_outcome": "processing"
    }
    _event_history.append(history_entry)

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

                    # Extract severity score
                    m = re.search(r'"severity_score"\s*:\s*(\d+)', part.text)
                    if m:
                        history_entry["severity_score"] = int(m.group(1))

                    if "AUTO-DISPATCH" in part.text:
                        history_entry["routing_decision"] = "auto_dispatch"
                        history_entry["final_outcome"] = "auto_dispatched"

                elif hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    interrupt_id = fc.args.get("interruptId", "unknown")
                    events_log.append(
                        f"[HITL] Workflow paused — interrupt_id={interrupt_id}"
                    )
        # ADK 2.0 signals HITL via long_running_tool_ids
        if hasattr(event, "long_running_tool_ids") and event.long_running_tool_ids:
            needs_approval = True
            history_entry["hitl_triggered"] = True
            history_entry["routing_decision"] = "review_agent"
            history_entry["final_outcome"] = "pending_approval"

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
    logger.info("Approve request — session=%s user=%s approved=%s", session_id, user_id, approved)

    resume_part = create_request_input_response(
        interrupt_id="dispatch_approval",
        response={"approved": approved},
    )

    # Update history
    for entry in _event_history:
        if entry["session_id"] == session_id:
            entry["final_outcome"] = "dispatched" if approved else "declined"
            break

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


# ---------------------------------------------------------------------------
# GET /history — retrieve event history
# ---------------------------------------------------------------------------
@fastapi_app.get("/history")
async def get_history():
    """Return the in-memory history of processed events."""
    return JSONResponse(content={"history": _event_history})


# ---------------------------------------------------------------------------
# DELETE /history — clear the in-memory event history
# ---------------------------------------------------------------------------
@fastapi_app.delete("/history")
async def clear_history():
    """Clear the in-memory event history for the current session."""
    _event_history.clear()
    logger.info("Event history cleared via DELETE /history")
    return JSONResponse(content={"status": "cleared", "count": 0})


@fastapi_app.get("/")
async def dashboard():
    """Serve the DispatchAI web dashboard."""
    return FileResponse("emergency_agent/dashboard.html")

@fastapi_app.get("/fleet")
async def get_fleet():
    """Proxy fleet status from the resource agent (via the mock fallback)."""
    from .resource_client import get_mock_fleet
    return JSONResponse(content=get_mock_fleet())

@fastapi_app.post("/reset-fleet")
async def reset_fleet():
    """Proxy fleet reset to the resource agent and reset mock state."""
    from .resource_client import reset_mock_fleet
    reset_mock_fleet()

    try:
        async with httpx.AsyncClient() as client:
            await client.post("http://localhost:8001/reset-fleet", timeout=3.0)
    except Exception:
        pass

    from .resource_client import get_mock_fleet
    return JSONResponse(content={"status": "reset", "fleet": get_mock_fleet()})
