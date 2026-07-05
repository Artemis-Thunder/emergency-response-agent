# ruff: noqa: E501
"""ResourceAvailabilityAgent — ADK agent with fleet inventory tools.

Maintains an in-memory fleet inventory and exposes two tools:
  • check_availability — query how many units of a given type are available
  • reserve_units     — decrement available count and return confirmation

Designed to be served over A2A so the main dispatch workflow can query
it before confirming resource allocation.
"""

import logging
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory fleet inventory (mock data)
# ---------------------------------------------------------------------------
FLEET: dict[str, dict[str, Any]] = {
    "fire": {"name": "Fire Trucks", "total": 5, "available": 4},
    "medical": {"name": "Ambulances", "total": 8, "available": 6},
    "crime": {"name": "Police Cars", "total": 10, "available": 7},
    "default": {"name": "General Units", "total": 6, "available": 5},
}


def _get_fleet_entry(incident_type: str) -> dict[str, Any]:
    """Resolve incident_type to a fleet entry, falling back to 'default'."""
    key = incident_type.lower().strip()
    return FLEET.get(key, FLEET["default"])


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------

def check_availability(incident_type: str, units_requested: int) -> dict:
    """Check how many units of a given type are available vs requested.

    Args:
        incident_type: The type of incident (fire, medical, crime, etc.).
        units_requested: The number of units the dispatch wants to send.

    Returns:
        A dict with unit_type, available, requested, and sufficient flag.
    """
    entry = _get_fleet_entry(incident_type)
    available = entry["available"]
    return {
        "unit_type": entry["name"],
        "available": available,
        "requested": units_requested,
        "sufficient": available >= units_requested,
        "can_dispatch": min(available, units_requested),
    }


def reserve_units(incident_type: str, units: int) -> dict:
    """Reserve (decrement) units from the fleet inventory.

    Args:
        incident_type: The type of incident (fire, medical, crime, etc.).
        units: The number of units to reserve.

    Returns:
        A dict confirming reservation with remaining count, or an error.
    """
    entry = _get_fleet_entry(incident_type)
    available = entry["available"]

    if units <= 0:
        return {"success": False, "error": "units must be > 0"}

    actual = min(units, available)
    entry["available"] -= actual

    logger.info(
        "RESERVE: %d %s reserved (%d remaining)",
        actual, entry["name"], entry["available"],
    )

    return {
        "success": True,
        "unit_type": entry["name"],
        "reserved": actual,
        "remaining": entry["available"],
        "shortfall": max(0, units - actual),
    }


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

RESOURCE_AGENT_INSTRUCTION = """You are the Resource Availability Agent for an
emergency dispatch centre. You manage the fleet inventory and respond to
queries about resource availability.

When asked to check availability, use the check_availability tool with the
incident type and number of units requested.

When asked to reserve units, use the reserve_units tool with the incident
type and number of units to reserve.

Always respond with the exact tool output — do not embellish or add
information beyond what the tools return."""

root_agent = LlmAgent(
    name="resource_availability",
    model="gemini-1.5-flash",
    instruction=RESOURCE_AGENT_INSTRUCTION,
    tools=[
        FunctionTool(check_availability),
        FunctionTool(reserve_units),
    ],
)
