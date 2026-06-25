# ruff: noqa: E501
"""Emergency Response Coordination Agent — ADK 2.0 Workflow Graph.

Graph topology:
    START → parse_event → severity_scorer (LLM) → route_by_severity
                                                       ├── "auto"   → auto_dispatch   (terminal)
                                                       └── "review" → human_review (HITL)
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
# Node 3: Deterministic severity router (runs AFTER LLM scoring)
# ---------------------------------------------------------------------------


def route_by_severity(ctx: Context, node_input: dict):
    """Route based on the LLM's severity_score vs SEVERITY_THRESHOLD.

    Every report has already been scored by severity_scorer at this point.
    urgency_claimed is NOT used for routing — only the LLM's assessment.

    Returns Event with route="auto" (score < threshold) or
    route="review" (score >= threshold).
    """
    severity = node_input.get("severity_score", 0)
    report = ctx.state.get("report", {})

    # Merge assessment + report into a single dict for downstream nodes
    merged = {"assessment": node_input, "report": report}

    if severity < SEVERITY_THRESHOLD:
        return Event(output=merged, route="auto")
    return Event(output=merged, route="review")


# ---------------------------------------------------------------------------
# Node 4a: Auto-dispatch (LLM severity_score < threshold)
# ---------------------------------------------------------------------------


def auto_dispatch(node_input: dict):
    """Instantly dispatch low-severity reports — scored by LLM, no human needed."""
    report = node_input.get("report", {})
    assessment = node_input.get("assessment", {})

    report_id = report.get("report_id", "UNKNOWN")
    severity = assessment.get("severity_score", 0)
    units = assessment.get("recommended_units", max(1, severity))

    if not re.match(r"^ER-\d{4}-\d{6}$", report_id):
        result = f"DISPATCH REJECTED: Invalid report ID '{report_id}'."
    else:
        result = (
            f"AUTO-DISPATCH CONFIRMED — Report {report_id}: "
            f"{units} unit(s) dispatched for severity-{severity} "
            f"{assessment.get('incident_type', 'unknown')} at "
            f"{report.get('location', 'unknown')}."
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
# Node 2: LLM severity scorer (runs on EVERY report)
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

The caller may include an "urgency_claimed" field — treat it as a useful
signal but NOT as ground truth. Always make your own independent severity
assessment based on the incident_type, description, and location. A caller
may understate urgency; your job is to catch that.

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
# Node 4b: Human dispatcher review (HITL via RequestInput)
# ---------------------------------------------------------------------------


@node(rerun_on_resume=True)
async def human_review(ctx: Context, node_input: dict):
    """Pause the workflow and wait for a human dispatcher's decision.

    First invocation: yields a RequestInput with the LLM assessment.
    On resume:        reads ctx.resume_inputs for the approval decision.

    node_input is the merged {assessment, report} dict from route_by_severity.
    """
    assessment = node_input.get("assessment", {})
    report = node_input.get("report", {})

    if not ctx.resume_inputs:
        # ── First run: present assessment, request approval ──────────
        assessment_text = (
            f"🚨 SEVERITY {assessment.get('severity_score', '?')} — "
            f"{assessment.get('incident_type', 'unknown').upper()}\n"
            f"Report: {report.get('report_id', 'N/A')}\n"
            f"Location: {report.get('location', 'N/A')}\n"
            f"Description: {report.get('description', 'N/A')}\n"
            f"Justification: {assessment.get('justification', 'N/A')}\n"
            f"Recommended units: {assessment.get('recommended_units', 0)}\n"
            f"Recommended response: {assessment.get('recommended_response', 'N/A')}\n\n"
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
            "assessment": assessment,
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
        (parse_event, severity_scorer),
        (severity_scorer, route_by_severity),
        (route_by_severity, {"auto": auto_dispatch, "review": human_review}),
        (human_review, record_outcome),
    ],
)

app = App(
    name="emergency_agent",
    root_agent=root_agent,
)
