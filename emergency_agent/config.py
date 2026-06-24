"""Centralised configuration for the Emergency Response Agent."""

# Severity threshold: reports with urgency_claimed below this value are
# auto-dispatched instantly. Reports at or above this value go through
# LLM severity scoring and human dispatcher approval.
SEVERITY_THRESHOLD = 3

# Gemini model used for the LLM severity-scoring node.
MODEL_NAME = "gemini-3.1-flash-lite"
