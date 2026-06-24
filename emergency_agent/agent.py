# ruff: noqa: E501
"""Emergency Response Coordination Agent — ADK 2.0 Workflow Graph.

Graph topology:
    START → parse_event → route_by_urgency
                              ├── "auto"   → auto_dispatch          (terminal)
                              └── "review" → severity_scorer (LLM)
                                                 → human_review (HITL)
                                                       → record_outcome (terminal)
"""

import base64
import json
import os
import re

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import Workflow, node
from google.genai import types

from .config import MODEL_NAME, SEVERITY_THRESHOLD
from .schemas import EmergencyReport, SeverityAssessment

# ---------------------------------------------------------------------------
# Environment bootstrap
# ---------------------------------------------------------------------------
load_dotenv()
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"


# ---------------------------------------------------------------------------
# Node 1: Parse incoming event
# ---------------------------------------------------------------------------


def parse_event(ctx: Context, node_input):
    """Extract the emergency report from the incoming event.

    Accepts two payload shapes:
      • Plain JSON:   {"data": {"report_id": "...", ...}}
      • Base64:       {"data": "<base64-encoded-json>"}

    START sends types.Content (no input_schema), so we pull the raw text
    from the first Part.

    On workflow resume, the report is already cached in state — skip
    re-parsing the (now-different) resume message.
    """
    # Resume path: report already parsed on the first run
    cached = ctx.state.get("report")
    if cached:
        return Event(output=cached)

    raw_text = node_input.parts[0].text
    payload = json.loads(raw_text)

    data = payload.get("data", payload)
    if isinstance(data, str):
        # Base64-encoded event-bus payload
        data = json.loads(base64.b64decode(data).decode("utf-8"))

    report = EmergencyReport(**data)

    return Event(
        output=report.model_dump(),
        state={"report": report.model_dump()},
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=(
                        f"📋 Report {report.report_id} received: "
                        f"{report.incident_type} at {report.location} "
                        f"(urgency claimed: {report.urgency_claimed})"
                    )
                )
            ],
        ),
    )



# ---------------------------------------------------------------------------
# Node 2: Deterministic severity router
# ---------------------------------------------------------------------------


def route_by_urgency(node_input: dict):
    """Route based on urgency_claimed vs SEVERITY_THRESHOLD.

    Returns an Event with route="auto" or route="review" so the
    conditional edges in the Workflow graph fire correctly.
    """
    urgency = node_input.get("urgency_claimed", 0)

    if urgency < SEVERITY_THRESHOLD:
        return Event(output=node_input, route="auto")
    return Event(output=node_input, route="review")


# ---------------------------------------------------------------------------
# Node 3a: Auto-dispatch (urgency_claimed < threshold)
# ---------------------------------------------------------------------------


def auto_dispatch(node_input: dict):
    """Instantly dispatch low-severity reports — no LLM, no human."""
    report_id = node_input["report_id"]
    severity = node_input["urgency_claimed"]
    units = max(1, severity)  # 1 unit for sev-1, 2 units for sev-2

    if not re.match(r"^ER-\d{4}-\d{6}$", report_id):
        result = f"DISPATCH REJECTED: Invalid report ID '{report_id}'."
    else:
        result = (
            f"AUTO-DISPATCH CONFIRMED — Report {report_id}: "
            f"{units} unit(s) dispatched for severity-{severity} "
            f"{node_input['incident_type']} at {node_input['location']}."
        )

    # Content event → renders in UI / playground
    yield Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"⚡ {result}")],
        ),
    )
    # Output event → workflow terminal output
    yield Event(
        output={
            "status": "auto_dispatched",
            "report_id": report_id,
            "severity": severity,
            "units": units,
            "result": result,
        }
    )


# ---------------------------------------------------------------------------
# Node 3b: LLM severity scorer (urgency_claimed >= threshold)
# ---------------------------------------------------------------------------

SCORER_INSTRUCTION = """You are the severity scoring engine for a metropolitan
emergency dispatch centre. Analyse the incoming incident report and produce
a structured severity assessment.

Incident types: fire, medical, crime, noise_complaint, lost_pet, traffic, other.

Severity scale (1-5):
  1 = Minor (noise complaint, lost pet, parking issue)
  2 = Low (non-emergency but needs attention)
  3 = Medium (potential safety concern, timely response needed)
  4 = High (active threat, immediate response required)
  5 = Critical (life-threatening, all available units respond)

Based on the report details, assign a severity_score, classify the
incident_type, provide a brief justification, recommend the number of
response units (0-10), and describe the recommended_response.

Be concise and factual. Lives may depend on your accuracy."""

severity_scorer = LlmAgent(
    name="severity_scorer",
    model=MODEL_NAME,
    instruction=SCORER_INSTRUCTION,
    output_schema=SeverityAssessment,
    output_key="assessment",
)


# ---------------------------------------------------------------------------
# Node 4: Human dispatcher review (HITL via RequestInput)
# ---------------------------------------------------------------------------


@node(rerun_on_resume=True)
async def human_review(ctx: Context, node_input: dict):
    """Pause the workflow and wait for a human dispatcher's decision.

    First invocation: yields a RequestInput with the LLM assessment.
    On resume:        reads ctx.resume_inputs for the approval decision.
    """
    report = ctx.state.get("report", {})

    if not ctx.resume_inputs:
        # ── First run: present assessment, request approval ──────────
        assessment_text = (
            f"🚨 SEVERITY {node_input.get('severity_score', '?')} — "
            f"{node_input.get('incident_type', 'unknown').upper()}\n"
            f"Report: {report.get('report_id', 'N/A')}\n"
            f"Location: {report.get('location', 'N/A')}\n"
            f"Description: {report.get('description', 'N/A')}\n"
            f"Justification: {node_input.get('justification', 'N/A')}\n"
            f"Recommended units: {node_input.get('recommended_units', 0)}\n"
            f"Recommended response: {node_input.get('recommended_response', 'N/A')}\n\n"
            f'Reply with: {{"approved": true}} or {{"approved": false}}'
        )
        yield RequestInput(
            interrupt_id="dispatch_approval",
            message=assessment_text,
        )
        return

    # ── Resumed: human has decided ───────────────────────────────────
    decision = ctx.resume_inputs.get("dispatch_approval", "")

    # Accept dict (programmatic) or string (typed in playground)
    approved = False
    if isinstance(decision, dict):
        approved = decision.get("approved", False)
    elif isinstance(decision, str):
        approved = "true" in decision.lower() or "approve" in decision.lower()

    yield Event(
        output={
            "approved": approved,
            "assessment": node_input,
            "report": report,
        }
    )


# ---------------------------------------------------------------------------
# Node 5: Record outcome & execute dispatch
# ---------------------------------------------------------------------------


def record_outcome(node_input: dict):
    """Log the final decision and dispatch resources if approved."""
    approved = node_input.get("approved", False)
    report = node_input.get("report", {})
    assessment = node_input.get("assessment", {})

    report_id = report.get("report_id", "UNKNOWN")
    severity = assessment.get("severity_score", 0)
    units = assessment.get("recommended_units", 0)

    if approved:
        if re.match(r"^ER-\d{4}-\d{6}$", report_id):
            result = (
                f"DISPATCH CONFIRMED — Report {report_id}: "
                f"{units} unit(s) dispatched for severity-{severity} "
                f"{assessment.get('incident_type', 'unknown')} at "
                f"{report.get('location', 'unknown')}."
            )
        else:
            result = f"DISPATCH REJECTED: Invalid report ID '{report_id}'."
    else:
        result = (
            f"DISPATCH DECLINED — Report {report_id} "
            f"(severity-{severity}) was not approved by dispatcher."
        )

    yield Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"📝 {result}")],
        ),
    )
    yield Event(
        output={
            "status": "dispatched" if approved else "declined",
            "report_id": report_id,
            "severity": severity,
            "units": units if approved else 0,
            "result": result,
        }
    )


# ---------------------------------------------------------------------------
# Workflow Graph
# ---------------------------------------------------------------------------

root_agent = Workflow(
    name="emergency_response",
    edges=[
        ("START", parse_event),
        (parse_event, route_by_urgency),
        (route_by_urgency, {"auto": auto_dispatch, "review": severity_scorer}),
        (severity_scorer, human_review),
        (human_review, record_outcome),
    ],
)

app = App(
    name="emergency_agent",
    root_agent=root_agent,
)
