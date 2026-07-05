# ruff: noqa: E501
"""Thin client for querying the ResourceAvailabilityAgent over A2A.

Provides two async functions:
  • query_availability(incident_type, units_requested) → dict
  • reserve_resources(incident_type, units) → dict

Uses the a2a SDK's proper types for the JSON-RPC request/response so the
message format is exactly what the server expects.

Falls back to mock data when the resource agent is unreachable.
"""

import json
import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RESOURCE_AGENT_BASE_URL = "http://localhost:8001"

# A2A RPC endpoint — the Starlette app registers its handler at "/"
_A2A_RPC_PATH = "/"

# ---------------------------------------------------------------------------
# Mock fallback — used when the A2A resource agent is not running
# ---------------------------------------------------------------------------
_MOCK_FLEET: dict[str, dict[str, Any]] = {}

def reset_mock_fleet():
    """Reset the mock fleet, syncing from the real agent if possible."""
    global _MOCK_FLEET
    try:
        import httpx
        with httpx.Client() as client:
            resp = client.get(f"{RESOURCE_AGENT_BASE_URL}/fleet", timeout=2.0)
            if resp.status_code == 200:
                _MOCK_FLEET = resp.json()
                logger.info("Mock fleet synced from real agent.")
                return
    except Exception:
        pass

    # Fallback if real agent is offline
    _MOCK_FLEET = {
        "fire":    {"name": "Fire Trucks",   "total": 5,  "available": 4},
        "medical": {"name": "Ambulances",    "total": 8,  "available": 6},
        "crime":   {"name": "Police Cars",   "total": 10, "available": 7},
        "default": {"name": "General Units", "total": 6,  "available": 5},
    }
    logger.info("Mock fleet initialized with defaults.")

# Sync on startup (Option A)
reset_mock_fleet()

def get_mock_fleet() -> dict:
    return _MOCK_FLEET


def _mock_check(incident_type: str, units_requested: int) -> dict:
    key   = incident_type.lower().strip()
    entry = _MOCK_FLEET.get(key, _MOCK_FLEET["default"])
    avail = entry["available"]
    return {
        "unit_type":   entry["name"],
        "available":   avail,
        "requested":   units_requested,
        "sufficient":  avail >= units_requested,
        "can_dispatch": min(avail, units_requested),
        "source": "mock_fallback",
    }


def _mock_reserve(incident_type: str, units: int) -> dict:
    key   = incident_type.lower().strip()
    entry = _MOCK_FLEET.get(key, _MOCK_FLEET["default"])
    actual = min(units, entry["available"])
    entry["available"] -= actual
    return {
        "success":   True,
        "unit_type": entry["name"],
        "reserved":  actual,
        "remaining": entry["available"],
        "shortfall": max(0, units - actual),
        "source": "mock_fallback",
    }


# ---------------------------------------------------------------------------
# A2A JSON-RPC client
# ---------------------------------------------------------------------------

def _build_jsonrpc_payload(message_text: str) -> dict:
    """Build a valid A2A message/send JSON-RPC request payload.

    Uses the exact schema the a2a SDK expects:
      - top-level: jsonrpc, id, method, params
      - params.message: kind, role, messageId, parts
      - each part: kind, text (+ nullable metadata, etc.)
    """
    return {
        "jsonrpc": "2.0",
        "id":      str(uuid.uuid4()),
        "method":  "message/send",
        "params": {
            "message": {
                "kind":              "message",
                "role":              "user",
                "messageId":         str(uuid.uuid4()),
                "contextId":         None,
                "taskId":            None,
                "referenceTaskIds":  None,
                "extensions":        None,
                "metadata":          None,
                "parts": [
                    {
                        "kind":     "text",
                        "text":     message_text,
                        "metadata": None,
                    }
                ],
            },
            "configuration": None,
            "metadata":      None,
        },
    }


def _extract_text_from_response(data: dict) -> str | None:
    """Pull the agent's text reply out of an A2A JSON-RPC success response.

    The result is either a Task (has 'status'/'artifacts') or a Message
    (has 'parts' directly).  Both are tried.
    """
    result = data.get("result")
    if not result:
        logger.warning("A2A response has no 'result' field: %s", data)
        return None

    # ── Message kind ─────────────────────────────────────────────────────
    if result.get("kind") == "message":
        for part in result.get("parts", []):
            if part.get("kind") == "text":
                return part["text"]

    # ── Task kind — text comes from the last message artifact ─────────────
    if result.get("kind") == "task":
        status = result.get("status", {})
        msg = status.get("message") or {}
        for part in msg.get("parts", []):
            if part.get("kind") == "text":
                return part["text"]
        # Also check artifacts
        for artifact in result.get("artifacts", []):
            for part in artifact.get("parts", []):
                if part.get("kind") == "text":
                    return part["text"]

    logger.warning("Could not extract text from A2A result: %s", result)
    return None


async def _a2a_send_message(message_text: str, timeout: float = 8.0) -> str | None:
    """Send a message to the resource agent via A2A JSON-RPC.

    Returns the agent's text response, or None on any failure.
    Logs the specific failure reason instead of silently swallowing it.
    """
    url     = f"{RESOURCE_AGENT_BASE_URL}{_A2A_RPC_PATH}"
    payload = _build_jsonrpc_payload(message_text)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=timeout)

        if resp.status_code != 200:
            logger.warning(
                "A2A RPC call failed — HTTP %d from %s. Body: %s",
                resp.status_code, url, resp.text[:400],
            )
            return None

        data = resp.json()

        # Surface JSON-RPC-level errors
        if "error" in data:
            logger.warning(
                "A2A RPC returned error: %s", data["error"]
            )
            return None

        text = _extract_text_from_response(data)
        if text is None:
            logger.warning("A2A response parsed but no text found. Full response: %s", json.dumps(data)[:800])
        return text

    except httpx.ConnectError:
        logger.warning("A2A resource agent not reachable at %s — using mock fallback", url)
        return None
    except Exception as exc:
        logger.warning("A2A call failed (%s: %s) — using mock fallback", type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def query_availability(incident_type: str, units_requested: int) -> dict:
    """Query resource availability — tries A2A agent, falls back to mock."""
    prompt = (
        f"Check availability for incident type '{incident_type}' "
        f"with {units_requested} units requested."
    )
    response_text = await _a2a_send_message(prompt)

    if response_text:
        try:
            result = json.loads(response_text)
            result["source"] = "a2a_agent"
            logger.info("A2A availability result: %s", result)
            return result
        except json.JSONDecodeError:
            logger.warning("A2A response is not JSON: %r — falling back to mock", response_text[:200])

    return _mock_check(incident_type, units_requested)


async def reserve_resources(incident_type: str, units: int) -> dict:
    """Reserve units — tries A2A agent, falls back to mock."""
    prompt = f"Reserve {units} units for incident type '{incident_type}'."
    response_text = await _a2a_send_message(prompt)

    if response_text:
        try:
            result = json.loads(response_text)
            result["source"] = "a2a_agent"
            logger.info("A2A reservation result: %s", result)
            return result
        except json.JSONDecodeError:
            logger.warning("A2A response is not JSON: %r — falling back to mock", response_text[:200])

    return _mock_reserve(incident_type, units)
