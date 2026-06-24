# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

# Load .env file (contains GOOGLE_API_KEY)
load_dotenv()

os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"


# ---------------------------------------------------------------------------
# Tool: Dispatch Emergency Resources
# ---------------------------------------------------------------------------


def dispatch_resources(report_id: str, severity: int, units: int) -> str:
    """Dispatches emergency response resources for a validated report.

    Validates the report ID format, severity score, and unit count before
    authorising the dispatch. This is the final step after triage and
    approval.

    Args:
        report_id: The incident report identifier, must match ER-XXXX-XXXXXX
                    (e.g. ER-2026-000142).
        severity: The severity score assigned during triage (1-5).
        units: The number of response units to dispatch (0-10).

    Returns:
        A confirmation string on success, or an error string describing the
        validation failure.
    """
    # Validate report_id format: ER-XXXX-XXXXXX
    if not re.match(r"^ER-\d{4}-\d{6}$", report_id):
        return (
            f"DISPATCH REJECTED: Invalid report ID '{report_id}'. "
            "Expected format: ER-XXXX-XXXXXX (e.g. ER-2026-000142)."
        )

    # Validate severity range
    if not isinstance(severity, int) or severity < 1 or severity > 5:
        return (
            f"DISPATCH REJECTED: Severity must be an integer between 1 and 5. "
            f"Received: {severity}."
        )

    # Validate units range
    if not isinstance(units, int) or units < 0 or units > 10:
        return (
            f"DISPATCH REJECTED: Units must be an integer between 0 and 10. "
            f"Received: {units}."
        )

    return (
        f"DISPATCH CONFIRMED — Report {report_id}: {units} unit(s) dispatched "
        f"for severity-{severity} incident."
    )


# ---------------------------------------------------------------------------
# Agent & App
# ---------------------------------------------------------------------------

INSTRUCTION = """You are the Emergency Response Coordination Agent for a
metropolitan dispatch centre. Your role is to triage incoming incident reports
by scoring their severity and coordinating the dispatch of response units.

Incident types: fire, medical, crime, noise_complaint, lost_pet, traffic, other.

Severity scale (1-5):
  1 = Minor (noise complaint, lost pet, parking issue)
  2 = Low (non-emergency but needs attention)
  3 = Medium (potential safety concern, timely response needed)
  4 = High (active threat, immediate response required)
  5 = Critical (life-threatening, all available units respond)

When given an incident report:
1. Classify the incident type.
2. Assign a severity score (1-5) with a brief justification.
3. Recommend an appropriate number of response units (0-10).
4. Use the dispatch_resources tool to dispatch if authorised.

Always be concise and factual. Lives may depend on your accuracy."""

root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-flash-latest",
        api_key=os.environ.get("GOOGLE_API_KEY"),
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=INSTRUCTION,
    tools=[dispatch_resources],
)

app = App(
    root_agent=root_agent,
    name="app",
)
