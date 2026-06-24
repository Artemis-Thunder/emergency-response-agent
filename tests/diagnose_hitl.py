"""Diagnostic: dump ALL event fields for the high-severity path."""

import asyncio
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from emergency_agent.agent import app
from google.adk.runners import InMemoryRunner
from google.genai import types


async def diagnose():
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="emergency_agent", user_id="test"
    )

    report = json.dumps(
        {
            "data": {
                "report_id": "ER-2026-000042",
                "incident_type": "fire",
                "description": "Smoke and flames visible from 3rd floor",
                "location": "456 Oak Avenue",
                "urgency_claimed": 4,
            }
        }
    )

    i = 0
    async for event in runner.run_async(
        user_id="test",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=report)]
        ),
    ):
        i += 1
        print(f"\n--- Event {i} ---")
        print(f"  type: {type(event).__name__}")
        for attr in sorted(dir(event)):
            if attr.startswith("_"):
                continue
            try:
                val = getattr(event, attr)
                if callable(val) and not isinstance(val, property):
                    continue
                if val is None or val == {} or val == [] or val == "":
                    continue
                print(f"  {attr}: {repr(val)[:200]}")
            except Exception:
                pass

    print(f"\nTotal events: {i}")


asyncio.run(diagnose())
