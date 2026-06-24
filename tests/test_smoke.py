"""Smoke tests for the Emergency Response Agent workflow."""

import asyncio
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from emergency_agent.agent import app
from google.adk.runners import InMemoryRunner
from google.adk.workflow.utils._workflow_hitl_utils import create_request_input_response
from google.genai import types


async def test_auto_dispatch():
    """Test: severity 1 noise complaint → auto-dispatch (no LLM, no human)."""
    print("=" * 60)
    print("TEST 1: Auto-dispatch path (urgency_claimed=1)")
    print("=" * 60)

    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="emergency_agent", user_id="test"
    )

    report = json.dumps(
        {
            "data": {
                "report_id": "ER-2026-000001",
                "incident_type": "noise_complaint",
                "description": "Loud music from apartment 4B",
                "location": "123 Main St",
                "urgency_claimed": 1,
            }
        }
    )

    results = []
    async for event in runner.run_async(
        user_id="test",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=report)]
        ),
    ):
        if event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    print(f"  [CONTENT] {part.text}")
        if event.output is not None:
            results.append(event.output)

    # Verify auto-dispatch happened
    final = results[-1] if results else {}
    assert final.get("status") == "auto_dispatched", f"Expected auto_dispatched, got: {final}"
    assert "CONFIRMED" in final.get("result", ""), f"Expected CONFIRMED in result"
    print(f"\n  ✅ PASS: Auto-dispatched with {final.get('units')} unit(s)")
    print()


async def test_hitl_pause():
    """Test: severity 4 fire → LLM scoring → HITL pause (RequestInput)."""
    print("=" * 60)
    print("TEST 2: HITL pause (urgency_claimed=4)")
    print("=" * 60)

    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="emergency_agent", user_id="test"
    )

    report = json.dumps(
        {
            "data": {
                "report_id": "ER-2026-000042",
                "incident_type": "fire",
                "description": "Smoke and flames visible from 3rd floor windows",
                "location": "456 Oak Avenue",
                "urgency_claimed": 4,
            }
        }
    )

    paused = False
    interrupt_id = None
    async for event in runner.run_async(
        user_id="test",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=report)]
        ),
    ):
        if event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    print(f"  [CONTENT] {part.text}")
                elif hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    print(f"  [HITL]    FunctionCall: {fc.name or 'request_input'}")
                    print(f"            interrupt_id: {fc.args.get('interruptId', 'N/A')}")
                    interrupt_id = fc.args.get("interruptId")

        # Detect HITL pause via long_running_tool_ids
        if hasattr(event, "long_running_tool_ids") and event.long_running_tool_ids:
            paused = True

    assert paused, "Expected workflow to pause for human review"
    assert interrupt_id == "dispatch_approval", f"Expected dispatch_approval, got: {interrupt_id}"
    print(f"\n  ✅ PASS: Workflow paused with interrupt_id='{interrupt_id}'")
    print()
    return runner, session


async def test_hitl_approve(runner, session):
    """Test: resume the paused workflow with approval → dispatch confirmed."""
    print("=" * 60)
    print("TEST 3: HITL resume with APPROVAL")
    print("=" * 60)

    # Resume with approval — use ADK's create_request_input_response utility
    resume_part = create_request_input_response(
        interrupt_id="dispatch_approval",
        response={"approved": True},
    )
    resume_msg = types.Content(role="user", parts=[resume_part])

    results = []
    async for event in runner.run_async(
        user_id="test",
        session_id=session.id,
        new_message=resume_msg,
    ):
        if event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    print(f"  [CONTENT] {part.text}")
        if event.output is not None:
            results.append(event.output)

    final = results[-1] if results else {}
    assert final.get("status") == "dispatched", f"Expected dispatched, got: {final}"
    assert "CONFIRMED" in final.get("result", ""), f"Expected CONFIRMED"
    print(f"\n  ✅ PASS: Dispatch confirmed after approval")
    print()


async def test_hitl_reject():
    """Test: resume with rejection → dispatch declined."""
    print("=" * 60)
    print("TEST 4: HITL resume with REJECTION")
    print("=" * 60)

    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="emergency_agent", user_id="test"
    )

    report = json.dumps(
        {
            "data": {
                "report_id": "ER-2026-000099",
                "incident_type": "crime",
                "description": "Reported break-in at closed business",
                "location": "789 Industrial Blvd",
                "urgency_claimed": 3,
            }
        }
    )

    # Phase 1: send report, expect HITL pause
    async for event in runner.run_async(
        user_id="test",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=report)]
        ),
    ):
        if event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    print(f"  [CONTENT] {part.text}")

    # Phase 2: resume with rejection
    resume_part = create_request_input_response(
        interrupt_id="dispatch_approval",
        response={"approved": False},
    )
    resume_msg = types.Content(role="user", parts=[resume_part])

    results = []
    async for event in runner.run_async(
        user_id="test",
        session_id=session.id,
        new_message=resume_msg,
    ):
        if event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    print(f"  [CONTENT] {part.text}")
        if event.output is not None:
            results.append(event.output)

    final = results[-1] if results else {}
    assert final.get("status") == "declined", f"Expected declined, got: {final}"
    assert "DECLINED" in final.get("result", ""), f"Expected DECLINED"
    print(f"\n  ✅ PASS: Dispatch declined after rejection")
    print()


async def main():
    # Test 1: Auto-dispatch
    await test_auto_dispatch()

    # Test 2: HITL pause
    runner, session = await test_hitl_pause()

    # Test 3: Resume with approval
    await test_hitl_approve(runner, session)

    # Test 4: Full reject flow
    await test_hitl_reject()

    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
