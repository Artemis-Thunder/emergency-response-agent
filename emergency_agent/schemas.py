"""Pydantic schemas for the Emergency Response Agent workflow."""

from pydantic import BaseModel, Field, field_validator

MAX_DESCRIPTION_LENGTH = 2000


class EmergencyReport(BaseModel):
    """Incoming incident report extracted from the event payload."""

    report_id: str
    incident_type: str
    description: str
    location: str
    urgency_claimed: int = Field(ge=1, le=5)

    @field_validator("description")
    @classmethod
    def cap_description_length(cls, v: str) -> str:
        """Truncate descriptions exceeding MAX_DESCRIPTION_LENGTH."""
        if len(v) > MAX_DESCRIPTION_LENGTH:
            return v[:MAX_DESCRIPTION_LENGTH] + " [TRUNCATED]"
        return v


class SeverityAssessment(BaseModel):
    """Structured output from the LLM severity-scoring node."""

    severity_score: int = Field(ge=1, le=5)
    incident_type: str
    justification: str
    recommended_units: int = Field(ge=0, le=10)
    recommended_response: str
