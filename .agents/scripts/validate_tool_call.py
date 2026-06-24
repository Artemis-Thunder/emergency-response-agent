#!/usr/bin/env python3
"""PreToolUse hook script for run_command validation.

Reads a JSON payload from stdin describing a run_command tool invocation,
inspects the command line for destructive patterns, and exits with:
  - 0  → allow the command
  - 1  → block the command (prints rejection reason to stderr)
"""

import json
import re
import sys

# ---------------------------------------------------------------------------
# Destructive command patterns (case-insensitive)
# ---------------------------------------------------------------------------
BLOCKED_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/",        # rm -rf / , rm -f /etc
    r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+/",            # rm -r /
    r"\bmkfs\b",                                     # mkfs (format disk)
    r"\bdd\s+.*of=/dev/",                            # dd of=/dev/sda
    r":\(\)\s*\{\s*:\|\:\s*&\s*\}\s*;",             # fork bomb
    r"\bchmod\s+-[a-zA-Z]*R[a-zA-Z]*\s+777\s+/",   # chmod -R 777 /
    r"\bcurl\b.*\|\s*(ba)?sh",                       # curl | sh (pipe to shell)
    r"\bwget\b.*\|\s*(ba)?sh",                       # wget | sh
    r"\b>\s*/dev/sd[a-z]",                           # write to raw disk
    r"\bshutdown\b",                                  # shutdown
    r"\breboot\b",                                     # reboot
    r"\binit\s+0\b",                                  # init 0 (halt)
]


def validate_command(command: str) -> str | None:
    """Check command against blocked patterns.

    Returns None if the command is safe, or a reason string if blocked.
    """
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"Blocked by security policy: matched pattern '{pattern}'"
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        # If we can't parse the input, fail-safe by blocking
        print("ERROR: Could not parse tool call payload.", file=sys.stderr)
        sys.exit(1)

    # Extract the command line from the payload
    # The payload structure may vary; handle common shapes
    command = ""
    if isinstance(payload, dict):
        command = (
            payload.get("command", "")
            or payload.get("CommandLine", "")
            or payload.get("input", {}).get("command", "")
            or payload.get("input", {}).get("CommandLine", "")
        )

    if not command:
        # No command found — allow (not a run_command call we recognise)
        sys.exit(0)

    reason = validate_command(command)
    if reason:
        print(f"BLOCKED: {reason}", file=sys.stderr)
        print(f"Command was: {command}", file=sys.stderr)
        sys.exit(1)

    # Command is safe
    sys.exit(0)


if __name__ == "__main__":
    main()
