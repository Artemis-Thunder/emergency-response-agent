# ruff: noqa: E501
"""Security utilities: PII redaction and prompt-injection detection.

All regex patterns are applied to the report *description* before any
downstream node (LLM or human) sees the data.
"""

import re
from typing import NamedTuple

# ---------------------------------------------------------------------------
# PII Redaction
# ---------------------------------------------------------------------------

# Each pattern maps a human-readable category to a compiled regex.
# Replacements use [REDACTED:<CATEGORY>] tokens so downstream nodes
# can see *that* something was removed, but not *what*.

PII_PATTERNS: dict[str, re.Pattern] = {
    "SSN": re.compile(
        r"\b\d{3}-\d{2}-\d{4}\b"          # 123-45-6789
        r"|\b\d{9}\b"                      # 123456789 (9 consecutive digits)
    ),
    "CREDIT_CARD": re.compile(
        r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"   # 1234-5678-9012-3456
    ),
    "EMAIL": re.compile(
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
    ),
    "PHONE": re.compile(
        r"\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b"  # US formats
    ),
    "ADDRESS": re.compile(
        # Street number + street name + street type (common US formats)
        r"\b\d{1,6}\s+[A-Za-z0-9\s]{2,30}\b\s+"
        r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Road|Rd"
        r"|Court|Ct|Place|Pl|Way|Circle|Cir|Terrace|Ter)\b\.?",
        re.IGNORECASE,
    ),
}


class RedactionResult(NamedTuple):
    """Result of PII redaction."""
    sanitized: str
    categories: set[str]  # which PII categories were found & redacted


def redact_pii(text: str) -> RedactionResult:
    """Scrub all known PII patterns from *text*.

    Returns the sanitized text and a set of categories that were redacted
    (e.g. {"SSN", "EMAIL"}).  If nothing was found the set is empty and
    the text is returned unchanged.
    """
    redacted_categories: set[str] = set()

    for category, pattern in PII_PATTERNS.items():
        if pattern.search(text):
            redacted_categories.add(category)
            text = pattern.sub(f"[REDACTED:{category}]", text)

    return RedactionResult(sanitized=text, categories=redacted_categories)


# ---------------------------------------------------------------------------
# Prompt-Injection Detection
# ---------------------------------------------------------------------------

# Heuristic patterns that indicate an attacker is trying to manipulate
# the LLM into ignoring its instructions or forcing a particular outcome.
# Each entry is (compiled_regex, short_description).

INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|rules?|prompts?)", re.I),
     "instruction override attempt"),
    (re.compile(r"you\s+are\s+now\b", re.I),
     "role reassignment"),
    (re.compile(r"system\s*prompt", re.I),
     "system prompt reference"),
    (re.compile(r"(override|bypass|disable|skip)\s+(the\s+)?(safety|security|rules?|scoring|review|gate|filter|threshold)", re.I),
     "safety bypass attempt"),
    (re.compile(r"(auto[- ]?dispatch|dispatch\s+immediately|severity\s*[=:]\s*1)", re.I),
     "forced dispatch/severity manipulation"),
    (re.compile(r"do\s+not\s+(score|review|flag|report|log|redact)", re.I),
     "suppression attempt"),
    (re.compile(r"\[\s*INST\s*\]|\[\s*SYSTEM\s*\]|<\s*/?system\s*>|<<\s*SYS\s*>>", re.I),
     "prompt delimiter injection"),
    (re.compile(r"(forget|disregard|discard)\s+(everything|all|your|the)\s+(instructions?|context|rules?|training)", re.I),
     "context erasure"),
    (re.compile(r"output\s+(only|just|exactly)\s*['\"]", re.I),
     "output forcing"),
    (re.compile(r"(pretend|act\s+as\s+if|assume)\s+(you\s+are|this\s+is|the\s+severity)", re.I),
     "scenario manipulation"),
]


class InjectionResult(NamedTuple):
    """Result of prompt-injection detection."""
    is_injection: bool
    matched_patterns: list[str]  # human-readable descriptions


def detect_injection(text: str) -> InjectionResult:
    """Scan *text* for prompt-injection patterns.

    Returns True + list of matched pattern descriptions if any
    injection heuristic fires.  Returns False + [] otherwise.
    """
    matches: list[str] = []

    for pattern, description in INJECTION_PATTERNS:
        if pattern.search(text):
            matches.append(description)

    return InjectionResult(is_injection=len(matches) > 0, matched_patterns=matches)
