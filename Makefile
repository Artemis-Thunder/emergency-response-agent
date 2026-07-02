# Emergency Response Agent — Local Development Makefile
# (On Windows without make, run the commands directly in PowerShell)

.PHONY: install playground test clean

# Install all dependencies via uv
install:
	uv sync

# Launch the ADK playground UI (http://localhost:8000)
playground:
	uv run adk web emergency_agent

# Run the full smoke test suite (10 tests)
test:
	uv run python tests/test_smoke.py

# Remove generated artifacts
clean:
	rm -f artifacts/audit_log.jsonl
	rm -rf __pycache__ emergency_agent/__pycache__ tests/__pycache__
