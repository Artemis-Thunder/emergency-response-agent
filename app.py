"""Gradio UI for the Emergency Response Agent.

Tab 1 — Submit Report: send events, see formatted workflow output, approve/decline HITL.
Tab 2 — Audit Log: stats bar + refresh table from GET /history.

Run:
    uv run python app.py
(Requires the main agent server already running on port 8080.)
"""

import json
import random
import re

import gradio as gr
import httpx

API          = "http://localhost:8080"
RESOURCE_API = "http://localhost:8001"

# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

_SEV_BADGE = {
    1: "🟢 LOW",
    2: "🟢 LOW",
    3: "🟡 MODERATE",
    4: "🟠 HIGH",
    5: "🔴 CRITICAL",
}

_ROUTING_BADGE = {
    "auto_dispatch": "⚡ auto_dispatch",
    "review_agent":  "👤 review_agent",
    "unknown":       "❓ unknown",
}


def _sev_badge(score) -> str:
    try:
        return _SEV_BADGE.get(int(score), f"❓ {score}")
    except (TypeError, ValueError):
        return "—"


def _sev_label(score) -> str:
    """'5 🔴' style label for the dataframe."""
    try:
        s = int(score)
        emoji = {1: "🟢", 2: "🟢", 3: "🟡", 4: "🟠", 5: "🔴"}.get(s, "")
        return f"{s} {emoji}"
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------------------
# Event-log formatter
# ---------------------------------------------------------------------------

_ASSESSMENT_RE = re.compile(
    r'"severity_score"\s*:\s*(\d+).*?"incident_type"\s*:\s*"([^"]+)"'
    r'.*?"recommended_units"\s*:\s*(\d+)'
    r'.*?"recommended_response"\s*:\s*"([^"]+)"',
    re.DOTALL,
)

_RESOURCE_RE = re.compile(
    r"\[Resource check \(([^)]+)\):\s*(.+?)\]"
)

_AUTO_DISPATCH_RE = re.compile(
    r"AUTO-DISPATCH CONFIRMED — (.*)"
)


def _fmt_single_event(raw: str, idx: int) -> str:
    """Format one workflow event into a clean human-readable block."""
    stripped = raw.strip()

    # ── LLM assessment JSON blob ──────────────────────────────────────────
    if '"severity_score"' in stripped:
        try:
            # Pull the JSON substring out (may be wrapped in prose)
            start = stripped.index("{")
            end   = stripped.rindex("}") + 1
            blob  = json.loads(stripped[start:end])
            score = blob.get("severity_score", "?")
            itype = blob.get("incident_type", "?")
            units = blob.get("recommended_units", "?")
            resp  = blob.get("recommended_response", "N/A")
            just  = blob.get("justification", "")
            lines = [
                f"  {idx}. 🧠 AI ASSESSMENT",
                f"     ├─ Severity Score:      {score}/5  {_sev_badge(score)}",
                f"     ├─ Incident Type:       {itype}",
                f"     ├─ Units Recommended:   {units}",
                f"     ├─ Justification:       {just}",
                f"     └─ Response:            {resp}",
            ]
            return "\n".join(lines)
        except Exception:
            pass  # fall through to default

    # ── Resource check line ───────────────────────────────────────────────
    m = _RESOURCE_RE.search(stripped)
    if m:
        source  = m.group(1)
        detail  = m.group(2)
        return f"  {idx}. 🚒 RESOURCE CHECK ({source}): {detail}"

    # ── Auto-dispatch confirmation ────────────────────────────────────────
    m = _AUTO_DISPATCH_RE.search(stripped)
    if m:
        return f"  {idx}. ⚡ AUTO-DISPATCH CONFIRMED — {m.group(1)}"

    # ── HITL pause marker ─────────────────────────────────────────────────
    if "[HITL]" in stripped:
        return f"  {idx}. 🔶 {stripped}"

    # ── Default pass-through ──────────────────────────────────────────────
    return f"  {idx}. {stripped}"


def _fmt_events(events: list[str]) -> str:
    if not events:
        return "  (no events returned)"
    return "\n".join(_fmt_single_event(e, i + 1) for i, e in enumerate(events))


def _extract_severity_from_events(events: list[str]) -> int | None:
    for e in events:
        if '"severity_score"' in e:
            m = re.search(r'"severity_score"\s*:\s*(\d+)', e)
            if m:
                return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gen_report_id() -> str:
    return f"ER-2026-{random.randint(0, 999_999):06d}"


# ---------------------------------------------------------------------------
# Tab 1 logic
# ---------------------------------------------------------------------------

def submit_report(description: str, incident_type: str, urgency: int):
    """POST /event and return (events_text, session_id, approve_visible, decline_visible)."""
    if not description.strip():
        return (
            "⚠️  Please enter a description.",
            "",
            gr.update(visible=False),
            gr.update(visible=False),
        )

    report = {
        "report_id":     _gen_report_id(),
        "description":   description.strip(),
        "incident_type": incident_type,
        "urgency_claimed": int(urgency),
        "location":      "Submitted via UI",
    }

    try:
        resp = httpx.post(f"{API}/event", json={"data": report}, timeout=60)
        data = resp.json()
    except Exception as exc:
        return (
            f"❌ Connection error: {exc}",
            "",
            gr.update(visible=False),
            gr.update(visible=False),
        )

    status     = data.get("status", "unknown")
    needs_hitl = data.get("needs_human_approval", False)
    session_id = data.get("session_id", "")
    events     = data.get("events", [])

    sev   = _extract_severity_from_events(events)
    badge = f"  |  Severity: {_sev_badge(sev)}" if sev is not None else ""
    sep   = "─" * 68

    header_parts = [
        f"Status: {status.upper()}{badge}",
        f"Session: {session_id}",
    ]
    header = "  |  ".join(header_parts) + f"\n{sep}\n"

    if needs_hitl:
        header += f"🔶 HITL TRIGGERED — awaiting dispatcher approval\n{sep}\n"

    output = header + _fmt_events(events)
    return output, session_id, gr.update(visible=needs_hitl), gr.update(visible=needs_hitl)


def _send_approval(session_id: str, approved: bool) -> str:
    if not session_id:
        return "⚠️  No active session — submit a report first."
    try:
        resp = httpx.post(
            f"{API}/approve",
            json={"session_id": session_id, "approved": approved},
            timeout=60,
        )
        data = resp.json()
    except Exception as exc:
        return f"❌ Connection error: {exc}"

    decision = data.get("decision", "unknown").upper()
    events   = data.get("events", [])
    sep      = "─" * 68
    header   = f"Decision: {decision}\n{sep}\n"
    return header + _fmt_events(events)


def approve(session_id: str):
    result = _send_approval(session_id, approved=True)
    return result, gr.update(visible=False), gr.update(visible=False)


def decline(session_id: str):
    result = _send_approval(session_id, approved=False)
    return result, gr.update(visible=False), gr.update(visible=False)


# ---------------------------------------------------------------------------
# Tab 2 logic
# ---------------------------------------------------------------------------

def refresh_history():
    """GET /history → (rows, stats_text, status_text)."""
    try:
        resp    = httpx.get(f"{API}/history", timeout=10)
        history = resp.json().get("history", [])
    except Exception as exc:
        return [], f"❌ Connection error: {exc}", ""

    # ── Stats ─────────────────────────────────────────────────────────────
    total     = len(history)
    auto_cnt  = sum(1 for e in history if e.get("routing_decision") == "auto_dispatch")
    review_cnt= sum(1 for e in history if e.get("routing_decision") == "review_agent")
    hitl_cnt  = sum(1 for e in history if e.get("hitl_triggered"))

    stats = (
        f"📊  Total: {total}   |   "
        f"⚡ Auto-Dispatched: {auto_cnt}   |   "
        f"👤 Human Review: {review_cnt}   |   "
        f"🔶 HITL Triggered: {hitl_cnt}"
    )

    # ── Rows ──────────────────────────────────────────────────────────────
    rows = []
    for entry in history:
        routing_raw = entry.get("routing_decision", "unknown")
        routing_display = _ROUTING_BADGE.get(routing_raw, routing_raw)
        hitl = "🔶 Yes" if entry.get("hitl_triggered") else "—"

        # Timestamp derived from session_id suffix if possible, else "—"
        session = entry.get("session_id", "")
        ts = "—"

        rows.append([
            entry.get("description", "")[:80],       # truncate long descriptions
            _sev_label(entry.get("severity_score")),
            routing_display,
            hitl,
            entry.get("final_outcome", ""),
            ts,
        ])

    info = f"Loaded {total} event(s)."
    return rows, stats, info


# ---------------------------------------------------------------------------
# Fleet status
# ---------------------------------------------------------------------------

_FLEET_EMOJI = {
    "fire":    "🚒",
    "medical": "🚑",
    "crime":   "🚓",
    "default": "🚐",
}


def fetch_fleet_status() -> str:
    """GET /fleet from the resource agent and format as readable text."""
    try:
        resp = httpx.get(f"{RESOURCE_API}/fleet", timeout=4)
        fleet = resp.json()
    except Exception:
        return "⚠️  Resource agent offline — fleet status unavailable."

    lines = []
    for key, val in fleet.items():
        emoji = _FLEET_EMOJI.get(key, "🚗")
        name  = val.get("name", key)
        avail = val.get("available", "?")
        total = val.get("total", "?")
        lines.append(f"  {emoji}  {name:<18} {avail} / {total} available")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reset session
# ---------------------------------------------------------------------------

def reset_session():
    """Clear history on main server + reset fleet on resource agent, then refresh."""
    messages = []

    # ── 1. Clear event history ────────────────────────────────────────────
    try:
        r = httpx.delete(f"{API}/history", timeout=5)
        if r.status_code == 200:
            messages.append("✅ History cleared")
        else:
            messages.append(f"⚠️ History clear returned HTTP {r.status_code} — restart main server to pick up DELETE /history")
    except Exception as exc:
        messages.append(f"❌ Main server unreachable: {exc}")

    # ── 2. Reset fleet ────────────────────────────────────────────────────
    try:
        r = httpx.post(f"{RESOURCE_API}/reset-fleet", timeout=5)
        if r.status_code == 200:
            messages.append("✅ Fleet reset")
        else:
            messages.append(f"⚠️ Fleet reset returned HTTP {r.status_code} — restart resource agent to pick up POST /reset-fleet")
    except Exception as exc:
        messages.append(f"❌ Resource agent unreachable: {exc}")

    # ── 3. Refresh display ────────────────────────────────────────────────
    rows, stats, _ = refresh_history()
    fleet_str = fetch_fleet_status()
    status_msg = "  |  ".join(messages)
    return rows, stats, status_msg, fleet_str


# ---------------------------------------------------------------------------
# Build UI
# ---------------------------------------------------------------------------

_DARK_CSS = """
/* ── Dark base ──────────────────────────────────────────── */
body, .gradio-container {
    background-color: #1a1a1a !important;
    color: #f0f0f0 !important;
}
/* panels / cards */
.block, .panel, .gap, .form, .gr-box, .gr-panel,
.tabitem, .tab-content, .svelte-1ipelgc {
    background-color: #242424 !important;
    border-color: #333 !important;
}
/* labels */
label, .label-wrap, span.svelte-1f354aw {
    color: #d0d0d0 !important;
}
/* inputs & textareas */
input, textarea, select, .gr-input, .gr-textarea {
    background-color: #2e2e2e !important;
    color: #f0f0f0 !important;
    border-color: #444 !important;
}
/* primary button → orange */
button.primary, .primary-btn, button[variant="primary"] {
    background-color: #ff6b35 !important;
    border-color: #ff6b35 !important;
    color: #fff !important;
}
button.primary:hover {
    background-color: #e85e2a !important;
}
/* stop/danger button → red (keep as-is, just ensure dark bg) */
button.stop, button[variant="stop"] {
    background-color: #c0392b !important;
    color: #fff !important;
}
/* secondary button */
button.secondary, button[variant="secondary"] {
    background-color: #333 !important;
    color: #ff6b35 !important;
    border-color: #ff6b35 !important;
}
/* tab bar */
.tab-nav button {
    color: #aaa !important;
    background: transparent !important;
    border-bottom: 2px solid transparent !important;
}
.tab-nav button.selected {
    color: #ff6b35 !important;
    border-bottom: 2px solid #ff6b35 !important;
}
/* slider track */
input[type=range]::-webkit-slider-thumb {
    background: #ff6b35 !important;
}
input[type=range]::-webkit-slider-runnable-track {
    background: #444 !important;
}
/* dataframe */
table th { background-color: #2a2a2a !important; color: #ff6b35 !important; }
table td { background-color: #1e1e1e !important; color: #e0e0e0 !important; }
table tr:nth-child(even) td { background-color: #242424 !important; }
/* dropdown options */
.dropdown-arrow, .wrap-inner { color: #f0f0f0 !important; background: #2e2e2e !important; }
"""

_theme = gr.themes.Soft(
    primary_hue="orange",
    neutral_hue="gray",
    text_size=gr.themes.sizes.text_sm,
)

with gr.Blocks(title="Emergency Response Agent", theme=_theme, css=_DARK_CSS) as demo:
    gr.Markdown("# 🚨 Emergency Response Agent")
    gr.Markdown(
        "Triage, severity-score, and dispatch emergency reports through the ADK 2.0 workflow. "
        "Requires the main server running on **port 8080**."
    )

    _session_state = gr.State("")

    # ══════════════════════════════════════════════════════════════════════
    with gr.Tab("Submit Report"):
        with gr.Row():
            # ── Left: Inputs ──────────────────────────────────────────────
            with gr.Column(scale=1):
                desc_input = gr.Textbox(
                    label="Incident Description",
                    placeholder="Describe the emergency in detail…",
                    lines=5,
                )
                type_input = gr.Dropdown(
                    label="Incident Type",
                    choices=["fire", "medical", "crime", "noise", "traffic", "other"],
                    value="other",
                )
                urgency_input = gr.Slider(
                    label="Urgency Claimed  (1 = low · 5 = critical)",
                    minimum=1, maximum=5, step=1, value=2,
                )
                submit_btn = gr.Button("🚀  Submit Report", variant="primary", size="lg")

            # ── Right: Outputs ────────────────────────────────────────────
            with gr.Column(scale=2):
                events_out = gr.Textbox(
                    label="Workflow Events Log",
                    lines=20,
                    interactive=False,
                    placeholder="Workflow output will appear here after submission…",
                    show_copy_button=True,
                )
                with gr.Row():
                    approve_btn = gr.Button(
                        "✅  Approve Dispatch", variant="primary", visible=False
                    )
                    decline_btn = gr.Button(
                        "❌  Decline Dispatch", variant="stop", visible=False
                    )
                hitl_out = gr.Textbox(
                    label="Dispatcher Decision Outcome",
                    lines=6,
                    interactive=False,
                )

        submit_btn.click(
            fn=submit_report,
            inputs=[desc_input, type_input, urgency_input],
            outputs=[events_out, _session_state, approve_btn, decline_btn],
        )
        approve_btn.click(
            fn=approve,
            inputs=[_session_state],
            outputs=[hitl_out, approve_btn, decline_btn],
        )
        decline_btn.click(
            fn=decline,
            inputs=[_session_state],
            outputs=[hitl_out, approve_btn, decline_btn],
        )

    # ══════════════════════════════════════════════════════════════════════
    with gr.Tab("Audit Log"):
        with gr.Row():
            refresh_btn = gr.Button("🔄  Refresh", variant="secondary")
            reset_btn   = gr.Button("🗑️  Reset Session", variant="stop")
            status_txt  = gr.Textbox(label="", interactive=False, max_lines=1, scale=3)

        fleet_txt = gr.Textbox(
            label="🚨 Fleet Status",
            interactive=False,
            lines=4,
            value="Press Refresh to load fleet status.",
        )

        stats_txt = gr.Textbox(
            label="Session Stats",
            interactive=False,
            max_lines=1,
            value="Press Refresh to load history.",
        )

        history_df = gr.Dataframe(
            headers=["Description", "Severity", "Routing", "HITL", "Outcome", "Timestamp"],
            datatype=["str", "str", "str", "str", "str", "str"],
            interactive=False,
            wrap=True,
        )

        def _refresh_all():
            rows, stats, info = refresh_history()
            fleet = fetch_fleet_status()
            return rows, stats, info, fleet

        refresh_btn.click(
            fn=_refresh_all,
            inputs=[],
            outputs=[history_df, stats_txt, status_txt, fleet_txt],
        )
        reset_btn.click(
            fn=reset_session,
            inputs=[],
            outputs=[history_df, stats_txt, status_txt, fleet_txt],
        )


if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860, share=False)
