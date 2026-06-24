"""Pydantic schemas for the Emergency Response Agent workflow."""

from pydantic import BaseModel, Field


class EmergencyReport(BaseModel):
    """Incoming incident report extracted from the event payload."""

    report_id: str
    incident_type: str
    description: str
    location: str
    urgency_claimed: int = Field(ge=1, le=5)


class SeverityAssessment(BaseModel):
    """Structured output from the LLM severity-scoring node."""

    severity_score: int = Field(ge=1, le=5)
    incident_type: str
    justification: str
    recommended_units: int = Field(ge=0, le=10)
    recommended_response: str
