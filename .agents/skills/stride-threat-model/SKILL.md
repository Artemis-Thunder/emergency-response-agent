---
name: stride-threat-model
description: Performs a systematic STRIDE threat modeling assessment on
  the current project's codebase and architecture. Use this when starting
  a new implementation phase or reviewing existing components.
---

# STRIDE Threat Modeling Skill

## Goal
Guide the agent to analyze the workspace directory structure, configuration
files, and code files to produce a structured `threat_model.md` assessment.

## Instructions
1. **Analyze System Boundaries**: Map the entry points (tools, workflows,
   prompts) and data storage layers.
2. **STRIDE Evaluation**: Evaluate the system against the six STRIDE pillars:
   - **Spoofing**: Are caller identity boundaries verified before executing
     sensitive tool logic?
   - **Tampering**: Can users manipulate data flows, parameters, or
     underlying state — e.g. forcing an auto-dispatch via a crafted report?
   - **Repudiation**: Are critical dispatch decisions securely logged?
   - **Information Disclosure**: Are we risking leakage of PII (SSNs,
     addresses, phone numbers) or raw stack traces?
   - **Denial of Service**: Are there rate limits on expensive LLM calls
     per incoming report?
   - **Elevation of Privilege**: Can an unauthenticated report source
     bypass human review to reach privileged dispatch actions?
3. **Output**: Generate a highly structured `threat_model.md` saved
   directly into the workspace root.
