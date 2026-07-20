# AGENTS.md — Workspace rules for the Employee Onboarding Orchestrator

## Project Context
Multi-agent LangGraph workflow for IT employee onboarding and offboarding.
**Stack:** Python 3.12 · LangGraph · Azure AI Foundry / OpenAI · FastAPI · Pydantic v2 · pytest

## Architecture Rules

### 1. State Management
- All agent state flows through `models/state.py` TypedDicts (`OnboardingState`, `LeaverState`)
- Never add mutable defaults to TypedDicts; use `state.get("key", default)` pattern
- Pydantic BaseModel objects must be serialised to dicts before storing in state (`.model_dump()`)
- Restore from dicts using `ChecklistItem(**dict)` etc.

### 2. Agent Node Functions
- Every agent node must accept a single TypedDict state argument and return a `dict` of partial state updates
- Never mutate the state dict in place — always return a new partial dict
- All node functions must handle exceptions gracefully and set `{"error": str(exc)}` on failure
- Logging: use `logger.info` / `logger.warning` / `logger.error` — never `print()`

### 3. LLM Usage
- All LLM access goes through `agents/llm.py:get_llm()` — never import LangChain models directly in agent files
- LLM prompts must instruct the model to return JSON where structured data is required
- Always strip markdown fences from LLM JSON responses before parsing
- Provide fallback behaviour when LLM calls fail (log error, use base data)

### 4. PowerShell Scripts
- Scripts are ARTIFACTS ONLY — never execute them with `subprocess`
- All templates live in `scripts/templates/*.ps1.j2`
- Template variables use the contexts defined in `provisioner.py:_build_*_context()`
- New templates must be added to `ONBOARDING_SCRIPTS` or `LEAVER_SCRIPTS` in `provisioner.py`

### 5. Checklist Matrices
- `checklists/onboarding_matrix.json` and `checklists/leaver_matrix.json` are the source of truth
- The AuditorAgent validates against these files — changes here affect audit pass/fail logic
- Leaver matrix MUST remain in `execution_order` sequence

### 6. Human Gate
- The human gate uses `langgraph.types.interrupt()` — this is the correct LangGraph v0.2+ API
- Resume via `Command(resume={"approved": bool, "notes": str})`
- The gate is configured with `interrupt_before=["human_gate"]` in the graph builder

### 7. Tests
- Tests in `tests/` must not call the LLM or execute PowerShell
- Tests that need LLM behaviour should mock `agents.llm.get_llm`
- Fixture data lives in `tests/fixtures/`

### 8. API
- All endpoints are synchronous (FastAPI will run them in a thread pool)
- The run registry (`_run_registry`) is in-memory — for production replace with a database
- Graph singleton instances are process-scoped and built in the FastAPI lifespan handler

## Development Setup
```bash
# 1. Copy env template
cp .env.example .env
# 2. Edit .env — set LLM_PROVIDER + credentials

# 3. Install
pip install -e ".[dev]"

# 4. Run API
uvicorn api.main:app --reload

# 5. Run tests (no LLM required)
pytest tests/ -v --tb=short
```

## Env Vars Quick Reference
| Variable | Required | Description |
|----------|----------|-------------|
| `LLM_PROVIDER` | No | `azure` (default) or `openai` |
| `AZURE_OPENAI_ENDPOINT` | If azure | Azure OpenAI resource URL |
| `AZURE_OPENAI_API_KEY` | If azure | Azure API key |
| `AZURE_OPENAI_DEPLOYMENT` | If azure | Deployment name (e.g. `gpt-4o`) |
| `OPENAI_API_KEY` | If openai | OpenAI API key |
| `DATABASE_URL` | No | SQLite path, default `sqlite:///./onboarding.db` |
| `LOG_LEVEL` | No | `INFO` (default) |
