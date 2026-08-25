"""
models/schemas.py
─────────────────────────────────────────────────────────────────────────────
FastAPI request / response schemas.

Kept separate from internal state models so the API surface is stable even
if internal graph state evolves.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator

from .state import LeaverReason

# ── Request schemas ───────────────────────────────────────────────────────────


class StarterForm(BaseModel):
    """New-starter intake form — primary input to the onboarding graph."""

    first_name: str = Field(..., min_length=1, max_length=64, examples=["Jane"])
    last_name: str = Field(..., min_length=1, max_length=64, examples=["Smith"])
    role: str = Field(..., min_length=1, max_length=128, examples=["Software Engineer"])
    department: str = Field(..., min_length=1, max_length=64, examples=["Engineering"])
    start_date: date = Field(..., examples=["2025-09-01"])
    manager_email: EmailStr = Field(..., examples=["john.doe@contoso.com"])
    email_prefix: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9._-]+$",
        examples=["jane.smith"],
        description="Username portion of the new UPN, e.g. jane.smith → jane.smith@contoso.com",
    )
    domain: str = Field(default="contoso.com", examples=["contoso.com"])
    licence_sku: str = Field(
        default="E3",
        examples=["E3", "E5", "F3"],
        description="M365 licence SKU to assign",
    )
    location: str = Field(default="UK", examples=["UK", "US", "DE"])
    additional_notes: str = Field(default="", max_length=1024)

    @field_validator("email_prefix")
    @classmethod
    def lowercase_prefix(cls, v: str) -> str:
        return v.lower()

    @property
    def upn(self) -> str:
        return f"{self.email_prefix}@{self.domain}"

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class LeaverForm(BaseModel):
    """Leaver intake form — primary input to the offboarding graph."""

    first_name: str = Field(..., min_length=1, max_length=64)
    last_name: str = Field(..., min_length=1, max_length=64)
    email: EmailStr = Field(..., examples=["jane.smith@contoso.com"])
    last_working_day: date = Field(..., examples=["2025-09-30"])
    manager_email: EmailStr = Field(..., examples=["john.doe@contoso.com"])
    reason: LeaverReason = Field(default=LeaverReason.RESIGNATION)
    data_transfer_recipient: EmailStr | None = Field(
        default=None,
        description="Email to receive OneDrive / mailbox data. Defaults to manager.",
    )
    hardware_return_required: bool = Field(default=True)
    additional_notes: str = Field(default="", max_length=1024)

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class ApprovalRequest(BaseModel):
    """Body for POST /approve/{run_id}."""

    approved: bool
    notes: str = Field(default="", max_length=2048)


# ── Response schemas ──────────────────────────────────────────────────────────


class RunCreatedResponse(BaseModel):
    run_id: str
    status: str
    message: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: str  # "running" | "awaiting_approval" | "complete" | "failed"
    approval_status: str | None = None
    checklist_count: int | None = None
    scripts_generated: int | None = None
    audit_passed: bool | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PackSummaryResponse(BaseModel):
    run_id: str
    form: dict[str, Any]
    checklist: list[dict[str, Any]]
    scripts: dict[str, str]
    comms: dict[str, Any]
    audit_report: dict[str, Any]
    download_url: str
