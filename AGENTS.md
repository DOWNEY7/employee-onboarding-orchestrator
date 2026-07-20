"""
AGENTS.md
─────────────────────────────────────────────────────────────────────────────
Agent workspace rules and conventions for the Employee Onboarding Orchestrator.
"""
# AGENTS.md — Workspace Rules

## Project Overview
Multi-agent employee onboarding/offboarding orchestrator built with LangGraph, FastAPI, and Azure AI Foundry.

## Architecture

```
api/main.py          FastAPI HTTP layer — entry point for all runs
graph/               LangGraph StateGraph definitions
agents/              Individual agent node implementations
  planner.py         Builds checklist from role/dept matrix
  provisioner.py     Renders PS1 templates, validates with PSScriptAnalyzer
  comms.py           Drafts welcome email + day-1 guide
  human_gate.py      LangGraph interrupt() — pauses for human review
  auditor.py         Validates pack against required-steps matrix
models/              Pydantic state and FastAPI schemas
checklists/          JSON step matrices (onboarding + leaver)
scripts/templates/   Jinja2 PowerShell script templates
core/                Config (pydantic-settings) + LLM factory
tests/               pytest suite (no LLM required)
```

## Key Conventions

### State
- All graph state flows through `OnboardingState` / `LeaverState` TypedDicts
- Pydantic models serialised to `dict` before writing to state (JSON-safe)
- Never store unserialisable objects in state

### LLM Calls
- All LLM calls go through `agents/llm.py::get_llm()` (cached singleton)
- LLM provider controlled by `LLM_PROVIDER` env var (`azure` | `openai`)
- Agents must handle LLM failures gracefully — fall back, don't crash graph

### Scripts
- PowerShell scripts are **ARTEFACTS ONLY** — never executed automatically
- All templates must include `ARTIFACT ONLY — NOT EXECUTED` disclaimer in the header
- Use `$PSCmdlet.ShouldProcess` for all state-changing operations

### Testing
- Tests in `tests/` must not require an LLM or `pwsh` to pass
- Mock LLM calls with `unittest.mock` or `monkeypatch`
- PSScriptAnalyzer validation skips gracefully when `pwsh` not on PATH

### Error Handling
- Each agent sets `state["error"]` on failure instead of raising
- Graph conditional edges check for `error` and route to `__end__` on failure
- Log all exceptions with `logger.exception()` — include run_id context

## Running Locally

```bash
# Install
pip install -e ".[dev]"

# Configure
cp .env.example .env
# Edit .env with your Azure OpenAI or OpenAI credentials

# Run API
uvicorn api.main:app --reload --port 8000

# Run tests
pytest tests/ -v --tb=short

# Lint
ruff check . && ruff format --check .
```

## API Quick Reference

```bash
# Start an onboarding run
curl -X POST http://localhost:8000/onboard \
  -H "Content-Type: application/json" \
  -d @tests/fixtures/sample_starter.json

# Check status (replace RUN_ID)
curl http://localhost:8000/status/RUN_ID

# Approve
curl -X POST http://localhost:8000/approve/RUN_ID \
  -H "Content-Type: application/json" \
  -d '{"approved": true, "notes": "Looks good"}'

# Download pack
curl http://localhost:8000/pack/RUN_ID/zip -o pack.zip
```
