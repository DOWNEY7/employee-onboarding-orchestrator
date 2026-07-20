"""
models/state.py
─────────────────────────────────────────────────────────────────────────────
Shared LangGraph state types and Pydantic domain models.

All graph nodes read from / write to these TypedDicts so LangGraph can
checkpoint them atomically in SQLite.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

# ── Enumerations ──────────────────────────────────────────────────────────────

class Priority(StrEnum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AuditSeverity(StrEnum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    INFO = "Info"


class LeaverReason(StrEnum):
    RESIGNATION = "Resignation"
    REDUNDANCY = "Redundancy"
    RETIREMENT = "Retirement"
    CONTRACT_END = "ContractEnd"
    TERMINATION = "Termination"
    OTHER = "Other"


# ── Domain models (Pydantic BaseModel — richer validation) ────────────────────

class ChecklistItem(BaseModel):
    """A single actionable step in an onboarding or offboarding checklist."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    step_number: int
    title: str
    description: str
    system: str                         # e.g. "Azure AD", "Exchange", "Intune"
    owner: str                          # e.g. "IT", "HR", "Manager"
    priority: Priority
    tags: list[str] = Field(default_factory=list)
    completed: bool = False
    notes: str = ""

    model_config = {"use_enum_values": True}


class CommsBundle(BaseModel):
    """Drafted communications artefacts."""

    welcome_email_subject: str = ""
    welcome_email_html: str = ""
    welcome_email_plain: str = ""
    day1_guide_markdown: str = ""


class AuditFinding(BaseModel):
    """A single finding from the AuditorAgent."""

    severity: AuditSeverity
    step_title: str
    reason: str
    recommendation: str = ""

    model_config = {"use_enum_values": True}


class AuditReport(BaseModel):
    """Full audit report returned by the AuditorAgent."""

    passed: bool
    total_required: int
    total_present: int
    findings: list[AuditFinding] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    auditor_notes: str = ""


# ── LangGraph state TypedDicts ────────────────────────────────────────────────
# TypedDicts are preferred by LangGraph for state — they serialise cleanly.
# We embed Pydantic models as dicts (model.model_dump()) so the checkpoint
# serialiser can handle them without custom reducers.

class OnboardingState(TypedDict, total=False):
    """Shared state flowing through the onboarding graph."""

    # ── Input ────────────────────────────────────────────────────────────────
    run_id: str
    form: dict[str, Any]                # StarterForm.model_dump()
    reviewer_notes: str                 # feedback from human gate on rejection

    # ── PlannerAgent outputs ──────────────────────────────────────────────────
    checklist: list[dict[str, Any]]     # list of ChecklistItem.model_dump()
    role_matrix: list[dict[str, Any]]   # loaded from onboarding_matrix.json

    # ── ProvisionerAgent outputs ──────────────────────────────────────────────
    scripts: dict[str, str]             # {script_name: rendered_ps1_content}
    script_validation: dict[str, Any]   # {script_name: {passed, warnings}}

    # ── CommsAgent outputs ────────────────────────────────────────────────────
    comms: dict[str, Any]               # CommsBundle.model_dump()

    # ── HumanApprovalGate ─────────────────────────────────────────────────────
    approval_status: str                # ApprovalStatus value
    approval_notes: str

    # ── AuditorAgent outputs ──────────────────────────────────────────────────
    audit_report: dict[str, Any]        # AuditReport.model_dump()

    # ── Control ───────────────────────────────────────────────────────────────
    error: str                          # set on any agent failure
    iteration: int                      # tracks planner re-runs after rejection


class LeaverState(TypedDict, total=False):
    """Shared state flowing through the leaver/offboarding graph."""

    # ── Input ────────────────────────────────────────────────────────────────
    run_id: str
    form: dict[str, Any]                # LeaverForm.model_dump()

    # ── PlannerAgent outputs ──────────────────────────────────────────────────
    checklist: list[dict[str, Any]]     # security-ordered deprovisioning steps
    role_matrix: list[dict[str, Any]]

    # ── ProvisionerAgent outputs ──────────────────────────────────────────────
    scripts: dict[str, str]
    script_validation: dict[str, Any]

    # ── CommsAgent outputs (leaver) ───────────────────────────────────────────
    comms: dict[str, Any]               # farewell / handover notes

    # ── HumanApprovalGate ─────────────────────────────────────────────────────
    approval_status: str
    approval_notes: str

    # ── AuditorAgent outputs ──────────────────────────────────────────────────
    audit_report: dict[str, Any]

    # ── Control ───────────────────────────────────────────────────────────────
    error: str
    iteration: int
