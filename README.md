# Employee Onboarding Orchestrator

> **Multi-agent LangGraph workflow** that takes a single new-starter (or leaver) form and generates a complete IT pack: provisioning checklist, PowerShell scripts, welcome email, Day-1 guide, and an auditor validation pass — with a human approval gate between planning and finalisation.

---

## Architecture

```
InputForm (StarterForm / LeaverForm)
    │
    ▼
[PlannerAgent]        — builds OnboardingChecklist from role/dept matrix + LLM enrichment
    │
    ▼
[ProvisionerAgent]    — renders PowerShell scripts from Jinja2 templates (artifact only)
    │
    ▼
[CommsAgent]          — drafts welcome email (HTML + plain) + Day-1 guide (Markdown)
    │
    ▼
[HumanApprovalGate]   ← INTERRUPT — human reviews pack, approves or rejects
    │ approved                  │ rejected → back to PlannerAgent (max 3 iterations)
    ▼
[AuditorAgent]        — validates pack against required-steps matrix, flags gaps
    │
    ▼
OutputPack (ZIP: checklist.json + scripts/ + email + guide + audit_report.json)
```

**Leaver mode** is a mirror sub-graph with 17 security-ordered de-provisioning steps (revoke tokens → disable → remove groups → remove licences → archive mailbox → …).

---

## Stack

| Layer | Technology |
|-------|-----------|
| Orchestration | LangGraph 0.2+ (StateGraph, interrupt, Command) |
| LLM | Azure OpenAI (gpt-4o) · OpenAI fallback (env toggle) |
| API | FastAPI + Uvicorn |
| Validation | Pydantic v2 |
| Templating | Jinja2 |
| Persistence | LangGraph SqliteSaver |
| Tests | pytest + pytest-asyncio |
| CI | GitHub Actions |

---

## Quick Start

### 1. Clone and install

```bash
git clone <repo-url>
cd "EMPLOYEE ONBOARDING ORCHESTRATOR (INTERNAL)"
pip install -e ".[dev]"
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — set LLM_PROVIDER and credentials (see .env.example)
```

### 3. Run the API

```bash
uvicorn api.main:app --reload
# → http://127.0.0.1:8000/docs
```

### 4. Run tests (no LLM required)

```bash
pytest tests/ -v --tb=short
```

---

## API Reference

### `POST /onboard`
Start an onboarding workflow. Returns immediately with a `run_id`.

```json
{
  "first_name": "Jane",
  "last_name": "Smith",
  "role": "Software Engineer",
  "department": "Engineering",
  "start_date": "2025-09-01",
  "manager_email": "john.doe@contoso.com",
  "email_prefix": "jane.smith",
  "domain": "contoso.com",
  "licence_sku": "E3",
  "location": "UK"
}
```

### `POST /offboard`
Start a leaver workflow.

```json
{
  "first_name": "John",
  "last_name": "Doe",
  "email": "john.doe@contoso.com",
  "last_working_day": "2025-09-30",
  "manager_email": "sarah.jones@contoso.com",
  "reason": "Resignation"
}
```

### `GET /status/{run_id}`
Poll run status: `running` → `awaiting_approval` → `complete` | `failed`.

### `POST /approve/{run_id}`
Submit human approval decision. Graph resumes from the human gate.

```json
{ "approved": true, "notes": "Pack looks correct." }
```

On rejection (`"approved": false`), graph routes back to the PlannerAgent with the reviewer notes and regenerates the pack (up to 3 iterations).

### `GET /pack/{run_id}`
Download the finalised pack as a ZIP archive.

### `GET /pack/{run_id}/summary`
Get the full pack as a structured JSON response.

### `GET /health`
Health check.

---

## Onboarding Checklist (25 steps)

All steps are applied to every role (`required_for: ["*"]`). The PlannerAgent uses an LLM to tailor descriptions to the specific role and may add role-specific steps.

| # | Step | System | Priority |
|---|------|--------|----------|
| 1 | Create M365 user account | Azure AD | **Critical** |
| 2 | Assign licence (E3/E5/F3) | M365 Admin | **Critical** |
| 3 | Add to security groups | Azure AD | **Critical** |
| 4 | Add to distribution lists | Exchange | High |
| 5 | Enable MFA | Azure AD | **Critical** |
| 6 | Conditional Access check | Azure AD | **Critical** |
| 7–9 | Shared mailbox / SharePoint / Teams | Various | High–Medium |
| 10–12 | Intune device enrolment + software | Intune | High–Medium |
| 13–14 | VPN + LOB app access | Network / Apps | High |
| 15–16 | ITSM ticket + HR record | ServiceDesk / HR | High–Medium |
| 17–18 | Welcome email + Day-1 guide | Comms | High |
| 19–20 | Manager call + IT induction | Comms / Training | Medium |
| 21 | Acceptable Use Policy | Compliance | **Critical** |
| 22 | Security awareness training | Training | High |
| 23–25 | Emergency contact / Asset register / Day-30 reminder | HR / IT / Manager | Low–Medium |

---

## Leaver Checklist (17 steps — security ordered)

| # | Step | Priority |
|---|------|----------|
| 1 | Revoke all sessions and tokens | **Critical** |
| 2 | Disable Azure AD account | **Critical** |
| 3 | Remove all security groups | **Critical** |
| 4–7 | Remove licences / convert mailbox / transfer OneDrive / remove MFA | High |
| 8–9 | Remove DLs and Teams | Medium |
| 10–12 | Wipe Intune device / VPN / LOB apps | High |
| 13–15 | HR departure / asset return / payroll | High–Medium |
| 16 | Export and archive audit log | **Critical** |
| 17 | De-provisioning sign-off | **Critical** |

---

## PowerShell Scripts

All scripts are **artifacts only** — they are generated for review and manual execution, never run automatically.

| Template | Script | Action |
|----------|--------|--------|
| `create_user.ps1.j2` | `01_create_user.ps1` | `New-MgUser` — create Entra ID account |
| `assign_licence.ps1.j2` | `02_assign_licence.ps1` | `Set-MgUserLicense` — assign E3/E5/F3 |
| `add_groups.ps1.j2` | `03_add_groups.ps1` | `New-MgGroupMember` — add to security groups |
| `configure_mfa.ps1.j2` | `04_configure_mfa.ps1` | Configure MFA methods |
| `deprovision_user.ps1.j2` | `01_deprovision_user.ps1` | Revoke sessions → disable → remove groups → remove licences |

All scripts use `#Requires -Modules Microsoft.Graph`, `[CmdletBinding(SupportsShouldProcess)]`, `Set-StrictMode -Version Latest`, and `$ErrorActionPreference = 'Stop'`.

---

## Project Structure

```
├── agents/
│   ├── llm.py              LLM factory (Azure OpenAI / OpenAI toggle)
│   ├── planner.py          PlannerAgent — matrix loading + LLM enrichment
│   ├── provisioner.py      ProvisionerAgent — Jinja2 script rendering + validation
│   ├── comms.py            CommsAgent — email + Day-1 guide drafting
│   ├── human_gate.py       HumanApprovalGate — LangGraph interrupt()
│   └── auditor.py          AuditorAgent — gap detection + audit report
├── graph/
│   ├── onboarding_graph.py LangGraph StateGraph (onboarding)
│   └── leaver_graph.py     LangGraph StateGraph (leaver)
├── models/
│   ├── state.py            Pydantic models + LangGraph TypedDict state
│   └── schemas.py          FastAPI request/response schemas
├── checklists/
│   ├── onboarding_matrix.json  25-step required matrix
│   └── leaver_matrix.json      17-step security-ordered leaver matrix
├── scripts/templates/      Jinja2 PowerShell templates
├── api/main.py             FastAPI app (5 endpoints)
├── tests/                  pytest test suite (no LLM required)
├── .github/workflows/ci.yml
├── pyproject.toml
└── .env.example
```

---

## Development Notes

- **Human gate**: Uses `interrupt_before=["human_gate"]` so the graph pauses *before* the gate executes. The API resumes it with `Command(resume=approval_dict)`.
- **SQLite persistence**: State is checkpointed to `onboarding.db` between calls, so the human gate survives process restarts.
- **LLM fallback**: If the LLM call fails, agents fall back to the base matrix data and log an error.
- **PSScriptAnalyzer**: If `pwsh` is on PATH, scripts are validated with PSScriptAnalyzer. Otherwise a regex-based check runs.
- **Max rejections**: After 3 consecutive rejections at the human gate, the graph terminates with an escalation error.

---

## License

MIT
