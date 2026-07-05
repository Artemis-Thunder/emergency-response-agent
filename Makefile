# Emergency Response Agent — Local Development Makefile
# On Windows without make, run the commands directly in PowerShell.

.PHONY: install playground serve serve-resources serve-all test generate-traces grade grade-traces eval clean

# Install all dependencies via uv
install:
	uv sync

# Launch the ADK playground UI (http://localhost:8000)
playground:
	uv run adk web emergency_agent

# Launch the ambient event-driven server (http://localhost:8080)
serve:
	uv run uvicorn emergency_agent.server:fastapi_app --host 0.0.0.0 --port 8080

# Launch the ResourceAvailabilityAgent A2A server (http://localhost:8001)
serve-resources:
	uv run uvicorn resource_agent.serve:app --host 0.0.0.0 --port 8001

# Launch both agents (resource agent in background, main agent in foreground)
serve-all:
	@echo "Starting ResourceAvailabilityAgent on port 8001..."
	uv run uvicorn resource_agent.serve:app --host 0.0.0.0 --port 8001 &
	@sleep 2
	@echo "Starting main Emergency Response Agent on port 8080..."
	uv run uvicorn emergency_agent.server:fastapi_app --host 0.0.0.0 --port 8080

# Run the full smoke test suite (10 tests)
test:
	uv run python tests/test_smoke.py

# Generate evaluation traces from the dataset
# PowerShell: uv run python tests/eval/generate_traces.py
generate-traces:
	uv run python tests/eval/generate_traces.py

# Grade traces using adk eval with the eval config
# PowerShell: uv run adk eval emergency_agent/ artifacts/traces/eval_set.json --config_file_path tests/eval/eval_config.json --print_detailed_results
grade:
	uv run adk eval emergency_agent/ artifacts/traces/eval_set.json --config_file_path tests/eval/eval_config.json --print_detailed_results

# Run both generate + grade in sequence
# PowerShell: run generate-traces then grade-traces commands above
eval: generate-traces grade-traces

# Grade traces using standalone LLM judge (no agent re-run)
# PowerShell: uv run python tests/eval/grade_traces.py
grade-traces:
	uv run python tests/eval/grade_traces.py

# Remove generated artifacts
clean:
	rm -f artifacts/audit_log.jsonl
	rm -rf artifacts/traces/
	rm -rf __pycache__ emergency_agent/__pycache__ tests/__pycache__
