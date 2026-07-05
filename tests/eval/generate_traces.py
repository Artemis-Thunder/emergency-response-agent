"""Generate evaluation traces from the basic-dataset.json scenarios.

Loads each scenario, runs it through the same InMemoryRunner used by
the smoke tests, intercepts HITL pauses (auto-responding with the
simulate_human / human_decision fields), and saves structured traces
to artifacts/traces/generated_traces.json.

Supports all 10 scenarios including:
  - Standard auto-dispatch / human-review routing
  - PII redaction (single and multi-PII-type)
  - Prompt injection (overt and hidden within legitimate reports)
  - Ambiguous severity with no meaningful location
  - Informal slang / mixed-language reports
  - Borderline severity-3 threshold cases

Usage:
    uv run python tests/eval/generate_traces.py
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_PATH = PROJECT_ROOT / "tests" / "eval" / "datasets" / "basic-dataset.json"
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "traces"
OUTPUT_PATH = OUTPUT_DIR / "generated_traces.json"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Import agent and runner
# ---------------------------------------------------------------------------
sys.path.insert(0, str(PROJECT_ROOT))

from emergency_agent.agent import app  # noqa: E402
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.adk.workflow.utils._workflow_hitl_utils import (  # noqa: E402
    create_request_input_response,
)
from google.genai import types  # noqa: E402


def _extract_severity(text: str) -> int | None:
    """Try to extract severity_score from a JSON blob in event text."""
    m = re.search(r'"severity_score"\s*:\s*(\d+)', text)
    return int(m.group(1)) if m else None


def _extract_pii_types(text: str) -> list[str]:
    """Extract PII redaction categories from security gate output."""
    m = re.search(r"PII redacted:\s*(.*?)\)", text)
    if m:
        return [t.strip() for t in m.group(1).split(",") if t.strip()]
    return []


async def run_scenario(runner: InMemoryRunner, scenario: dict) -> dict:
    """Run a single evaluation scenario and return a structured trace."""
    report = scenario["report"]
    scenario_id = scenario["scenario_id"]
    simulate_human = scenario.get("simulate_human", False)
    human_decision = scenario.get("human_decision", False)

    logger.info("▶ Running scenario: %s", scenario_id)

    # Create session
    session = await runner.session_service.create_session(
        app_name="emergency_agent", user_id="eval"
    )

    # Build the input message (same shape as server.py /event)
    payload = json.dumps({"data": report})

    # --- Phase 1: Initial run ---
    events_log = []
    paused = False
    severity_score = None
    pii_redacted = []
    injection_detected = False
    llm_bypassed = False

    async for event in runner.run_async(
        user_id="eval",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=payload)]
        ),
    ):
        if event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    events_log.append({"type": "content", "text": part.text})

                    # Extract severity
                    s = _extract_severity(part.text)
                    if s is not None:
                        severity_score = s

                    # Extract PII types
                    pii = _extract_pii_types(part.text)
                    if pii:
                        pii_redacted.extend(pii)

                    # Detect injection
                    if "injection detected" in part.text.lower():
                        injection_detected = True
                        llm_bypassed = True

                    # Detect auto-dispatch
                    if "AUTO-DISPATCH" in part.text:
                        events_log.append(
                            {"type": "route", "route": "auto_dispatched"}
                        )

                elif hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    interrupt_id = fc.args.get("interruptId", "unknown")
                    events_log.append(
                        {
                            "type": "hitl_pause",
                            "function": fc.name or "request_input",
                            "interrupt_id": interrupt_id,
                        }
                    )

        if hasattr(event, "long_running_tool_ids") and event.long_running_tool_ids:
            paused = True

    # --- Phase 2: HITL resume (if paused and simulate_human is set) ---
    actual_route = "auto_dispatched"
    final_decision = "auto_dispatched"

    if paused:
        actual_route = "human_review"

        if simulate_human:
            resume_part = create_request_input_response(
                interrupt_id="dispatch_approval",
                response={"approved": human_decision},
            )

            async for event in runner.run_async(
                user_id="eval",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[resume_part]),
            ):
                if event.content:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            events_log.append({"type": "content", "text": part.text})

            final_decision = "dispatched" if human_decision else "declined"
        else:
            final_decision = "pending_approval"

    # Deduplicate PII types
    pii_redacted = sorted(set(pii_redacted))

    trace = {
        "scenario_id": scenario_id,
        "description": scenario.get("description", ""),
        "report": report,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # Expected values (from dataset)
        "expected_route": scenario.get("expected_route"),
        "expected_severity_min": scenario.get("expected_severity_min"),
        "expected_severity_max": scenario.get("expected_severity_max"),
        "expected_pii_redacted": scenario.get("expected_pii_redacted", []),
        "expected_injection_detected": scenario.get(
            "expected_injection_detected", False
        ),
        "expected_llm_bypassed": scenario.get("expected_llm_bypassed", False),
        # Actual observed values
        "actual_route": actual_route,
        "severity_score": severity_score,
        "urgency_claimed": report.get("urgency_claimed"),
        "pii_redacted": pii_redacted,
        "injection_detected": injection_detected,
        "llm_bypassed": llm_bypassed,
        "simulate_human": simulate_human,
        "human_decision": human_decision,
        "final_decision": final_decision,
        # Raw event log for deep inspection
        "events": events_log,
    }

    # ── Determine pass/fail status for the summary ──────────────────
    route_ok = actual_route == scenario.get("expected_route")
    sev_min = scenario.get("expected_severity_min")
    sev_max = scenario.get("expected_severity_max")
    sev_ok = True
    if severity_score is not None:
        if sev_min is not None and severity_score < sev_min:
            sev_ok = False
        if sev_max is not None and severity_score > sev_max:
            sev_ok = False
    injection_ok = injection_detected == scenario.get("expected_injection_detected", False)
    status = "✅" if (route_ok and sev_ok and injection_ok) else "⚠️"

    logger.info(
        "  %s route=%s (expected=%s) severity=%s injection=%s llm_bypassed=%s",
        status,
        actual_route,
        scenario.get("expected_route"),
        severity_score,
        injection_detected,
        llm_bypassed,
    )

    return trace


async def main():
    """Load dataset, run all scenarios, save traces."""
    # Load dataset
    with open(DATASET_PATH, encoding="utf-8") as f:
        scenarios = json.load(f)

    logger.info("Loaded %d scenarios from %s", len(scenarios), DATASET_PATH.name)

    # Create a single runner (same pattern as server.py)
    runner = InMemoryRunner(app=app)

    # Run all scenarios
    traces = []
    for scenario in scenarios:
        trace = await run_scenario(runner, scenario)
        traces.append(trace)

    # Save output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(traces, f, indent=2, ensure_ascii=False)

    logger.info("")
    logger.info("=" * 60)
    logger.info("TRACE GENERATION COMPLETE")
    logger.info("=" * 60)
    logger.info("  Scenarios: %d", len(traces))
    logger.info("  Output:    %s", OUTPUT_PATH)

    # Print summary table
    logger.info("")
    logger.info("%-28s %-18s %-18s %-6s %-9s %s", "SCENARIO", "EXPECTED", "ACTUAL", "SEV", "INJECT", "STATUS")
    logger.info("-" * 92)
    for t in traces:
        route_ok = t["actual_route"] == t["expected_route"]
        sev = t.get("severity_score")
        sev_min = t.get("expected_severity_min")
        sev_max = t.get("expected_severity_max")
        sev_ok = True
        if sev is not None:
            if sev_min is not None and sev < sev_min:
                sev_ok = False
            if sev_max is not None and sev > sev_max:
                sev_ok = False
        inject_ok = t.get("injection_detected") == t.get("expected_injection_detected", False)
        passed = route_ok and sev_ok and inject_ok
        status = "✅ PASS" if passed else "❌ FAIL"
        logger.info(
            "%-28s %-18s %-18s %-6s %-9s %s",
            t["scenario_id"],
            t["expected_route"],
            t["actual_route"],
            sev or "-",
            str(t.get("injection_detected", False)),
            status,
        )

    # Also generate adk-eval compatible format using proper ADK types
    from google.adk.evaluation.eval_case import EvalCase, Invocation
    from google.adk.evaluation.local_eval_sets_manager import EvalSet
    import time

    adk_eval_path = OUTPUT_DIR / "eval_set.json"
    eval_cases = []
    for t in traces:
        # Build the prompt from the report
        prompt_text = json.dumps({"data": t["report"]})
        # Build the response from the events
        response_parts = [e["text"] for e in t["events"] if e.get("type") == "content"]
        response_text = "\n".join(response_parts)

        invocation = Invocation(
            invocation_id=f"{t['scenario_id']}_inv_0",
            user_content=types.Content(
                role="user",
                parts=[types.Part.from_text(text=prompt_text)],
            ),
            final_response=types.Content(
                role="model",
                parts=[types.Part.from_text(text=response_text)],
            ),
            creation_timestamp=time.time(),
        )

        eval_case = EvalCase(
            eval_id=t["scenario_id"],
            conversation=[invocation],
            creation_timestamp=time.time(),
        )
        eval_cases.append(eval_case)

    eval_set = EvalSet(
        eval_set_id="emergency_agent_basic",
        name="Emergency Agent Basic Eval",
        description="Basic evaluation scenarios for routing, PII, and injection",
        eval_cases=eval_cases,
        creation_timestamp=time.time(),
    )

    with open(adk_eval_path, "w", encoding="utf-8") as f:
        f.write(eval_set.model_dump_json(indent=2, exclude_none=True))

    logger.info("")
    logger.info("ADK eval set written to: %s", adk_eval_path)


if __name__ == "__main__":
    asyncio.run(main())
