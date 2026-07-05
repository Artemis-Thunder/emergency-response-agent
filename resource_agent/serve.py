# ruff: noqa: E501
"""Serve the ResourceAvailabilityAgent over A2A on port 8001.

Also exposes two plain HTTP management routes on the same Starlette app:
  GET  /fleet        — current fleet inventory as JSON
  POST /reset-fleet  — reset all available counts to original defaults
  GET  /health       — liveness check

Usage:
    uv run uvicorn resource_agent.serve:app --host 0.0.0.0 --port 8001
"""

import json
import logging
import os

from dotenv import load_dotenv
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .agent import FLEET, root_agent

# ---------------------------------------------------------------------------
# Environment bootstrap
# ---------------------------------------------------------------------------
load_dotenv()
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
os.environ["ADK_OTEL_TO_CLOUD"] = "False"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Capture fleet defaults once at import time for reset
# ---------------------------------------------------------------------------
_FLEET_DEFAULTS: dict = {
    key: {"name": val["name"], "total": val["total"], "available": val["available"]}
    for key, val in FLEET.items()
}


# ---------------------------------------------------------------------------
# Extra route handlers (plain HTTP — no A2A involved)
# ---------------------------------------------------------------------------

async def handle_fleet(request: Request) -> JSONResponse:
    """GET /fleet — return current fleet availability snapshot."""
    snapshot = {
        key: {"name": val["name"], "total": val["total"], "available": val["available"]}
        for key, val in FLEET.items()
    }
    return JSONResponse(snapshot)


async def handle_reset_fleet(request: Request) -> JSONResponse:
    """POST /reset-fleet — restore all fleet counts to original defaults."""
    for key, defaults in _FLEET_DEFAULTS.items():
        FLEET[key]["available"] = defaults["available"]
    logger.info(
        "Fleet reset to defaults: %s",
        {k: FLEET[k]["available"] for k in FLEET},
    )
    snapshot = {
        key: {"name": val["name"], "total": val["total"], "available": val["available"]}
        for key, val in FLEET.items()
    }
    return JSONResponse({"status": "reset", "fleet": snapshot})


async def handle_health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Build the A2A Starlette app and inject extra routes
# ---------------------------------------------------------------------------
# to_a2a() returns a Starlette app; routes are appended during its lifespan
# startup.  We can safely add our extra routes BEFORE startup because Starlette
# resolves routes dynamically — they don't need to be present at construction.
app = to_a2a(root_agent, host="localhost", port=8001, protocol="http")

# Prepend management routes so they take priority over the A2A catch-all
app.routes.insert(0, Route("/fleet",       handle_fleet,       methods=["GET"]))
app.routes.insert(1, Route("/reset-fleet", handle_reset_fleet, methods=["POST"]))
app.routes.insert(2, Route("/health",      handle_health,      methods=["GET"]))

logger.info("ResourceAvailabilityAgent A2A server ready (use --port 8001)")
logger.info("Extra routes: GET /fleet  POST /reset-fleet  GET /health")
