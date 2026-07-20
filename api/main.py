"""
api/main.py
─────────────────────────────────────────────────────────────────────────────
FastAPI application for the Employee Onboarding Orchestrator.

Endpoints:
  POST /onboard           — Start an onboarding workflow
  POST /offboard          — Start a leaver/offboarding workflow
  GET  /status/{run_id}   — Poll run status
  POST /approve/{run_id}  — Submit human approval/rejection decision
  GET  /pack/{run_id}     — Download finalised pack as ZIP

Architecture:
  - LangGraph graphs are built once at startup (process-scoped singleton)
  - SQLite checkpointer persists graph state between API calls
  - A simple in-memory run registry (dict) maps run_id → metadata
    (for production, swap with a proper DB)
  - Graph execution is synchronous per-request (FastAPI async wrapper)
    For long-running graphs, consider a background task queue (Celery/ARQ)
"""

from __future__ import annotations

import io
import json
import logging
import os
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.types import Command

from graph.onboarding_graph import build_onboarding_graph
from graph.leaver_graph import build_leaver_graph
from models.schemas import (
    ApprovalRequest,
    LeaverForm,
    PackSummaryResponse,
    RunCreatedResponse,
    RunStatusResponse,
    StarterForm,
)

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Process-scoped singletons ─────────────────────────────────────────────────
_onboarding_graph = None
_onboarding_checkpointer = None
_leaver_graph = None
_leaver_checkpointer = None

# Simple in-memory run registry {run_id: RunRecord}
_run_registry: dict[str, dict[str, Any]] = {}


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build graphs on startup; clean up on shutdown."""
    global _onboarding_graph, _onboarding_checkpointer
    global _leaver_graph, _leaver_checkpointer

    logger.info("API: building LangGraph graphs...")
    _onboarding_graph, _onboarding_checkpointer = build_onboarding_graph()
    _leaver_graph, _leaver_checkpointer = build_leaver_graph()
    logger.info("API: graphs ready")

    yield  # ← app is running

    logger.info("API: shutting down")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Employee Onboarding Orchestrator",
    description=(
        "Multi-agent LangGraph workflow for IT employee onboarding and offboarding. "
        "Generates PowerShell scripts, welcome emails, Day-1 guides, and audit reports."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _new_run_id() -> str:
    return str(uuid.uuid4())


def _register_run(run_id: str, mode: str, form: dict) -> dict:
    record = {
        "run_id": run_id,
        "mode": mode,                       # "onboarding" | "leaver"
        "form": form,
        "status": "running",
        "approval_status": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }
    _run_registry[run_id] = record
    return record


def _update_run(run_id: str, **kwargs) -> None:
    if run_id in _run_registry:
        _run_registry[run_id].update(kwargs)
        _run_registry[run_id]["updated_at"] = datetime.now(timezone.utc).isoformat()


def _get_graph_state(run_id: str, mode: str) -> dict | None:
    """Retrieve the current LangGraph state snapshot for a run."""
    config = {"configurable": {"thread_id": run_id}}
    try:
        if mode == "onboarding":
            snapshot = _onboarding_graph.get_state(config)
        else:
            snapshot = _leaver_graph.get_state(config)
        return dict(snapshot.values) if snapshot else None
    except Exception as exc:
        logger.error("Failed to get graph state for %s: %s", run_id, exc)
        return None


def _build_zip(run_id: str, graph_state: dict) -> bytes:
    """Build a ZIP archive of the finalised onboarding/leaver pack."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        form = graph_state.get("form", {})
        checklist = graph_state.get("checklist", [])
        scripts = graph_state.get("scripts", {})
        comms = graph_state.get("comms", {})
        audit = graph_state.get("audit_report", {})

        # checklist.json
        zf.writestr(
            f"{run_id}/checklist.json",
            json.dumps(checklist, indent=2, default=str),
        )

        # audit_report.json
        zf.writestr(
            f"{run_id}/audit_report.json",
            json.dumps(audit, indent=2, default=str),
        )

        # welcome_email.html + .txt
        if comms.get("welcome_email_html"):
            zf.writestr(f"{run_id}/welcome_email.html", comms["welcome_email_html"])
        if comms.get("welcome_email_plain"):
            zf.writestr(f"{run_id}/welcome_email.txt", comms["welcome_email_plain"])

        # day1_guide.md
        if comms.get("day1_guide_markdown"):
            zf.writestr(f"{run_id}/day1_guide.md", comms["day1_guide_markdown"])

        # PowerShell scripts
        for script_name, content in scripts.items():
            zf.writestr(f"{run_id}/scripts/{script_name}", content)

        # metadata
        meta = {
            "run_id": run_id,
            "form": form,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        zf.writestr(f"{run_id}/metadata.json", json.dumps(meta, indent=2, default=str))

    buf.seek(0)
    return buf.read()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post(
    "/onboard",
    response_model=RunCreatedResponse,
    status_code=202,
    summary="Start an onboarding workflow",
    tags=["Onboarding"],
)
def start_onboarding(form: StarterForm) -> RunCreatedResponse:
    """
    Accept a new-starter form and start the onboarding workflow.

    The graph will run until the human approval gate, then pause.
    Poll `GET /status/{run_id}` to check state.
    Submit approval via `POST /approve/{run_id}`.
    """
    run_id = _new_run_id()
    record = _register_run(run_id, "onboarding", form.model_dump(mode="json"))

    config = {"configurable": {"thread_id": run_id}}
    initial_state: dict[str, Any] = {
        "run_id": run_id,
        "form": form.model_dump(mode="json"),
        "iteration": 0,
        "error": "",
    }

    logger.info("API: starting onboarding run %s for %s", run_id, form.display_name)

    try:
        # Run graph until first interrupt (human_gate)
        result = _onboarding_graph.invoke(initial_state, config=config)
        _update_run(run_id, status="awaiting_approval")
        logger.info("API: run %s paused at human_gate", run_id)
    except Exception as exc:
        logger.error("API: onboarding run %s failed: %s", run_id, exc)
        _update_run(run_id, status="failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Graph execution failed: {exc}") from exc

    return RunCreatedResponse(
        run_id=run_id,
        status="awaiting_approval",
        message=(
            f"Onboarding pack generated for {form.display_name}. "
            f"Awaiting human approval. POST /approve/{run_id} to proceed."
        ),
    )


@app.post(
    "/offboard",
    response_model=RunCreatedResponse,
    status_code=202,
    summary="Start a leaver/offboarding workflow",
    tags=["Offboarding"],
)
def start_offboarding(form: LeaverForm) -> RunCreatedResponse:
    """
    Accept a leaver form and start the de-provisioning workflow.
    """
    run_id = _new_run_id()
    record = _register_run(run_id, "leaver", form.model_dump(mode="json"))

    config = {"configurable": {"thread_id": run_id}}
    initial_state: dict[str, Any] = {
        "run_id": run_id,
        "form": form.model_dump(mode="json"),
        "iteration": 0,
        "error": "",
    }

    logger.info("API: starting leaver run %s for %s", run_id, form.display_name)

    try:
        _leaver_graph.invoke(initial_state, config=config)
        _update_run(run_id, status="awaiting_approval")
    except Exception as exc:
        logger.error("API: leaver run %s failed: %s", run_id, exc)
        _update_run(run_id, status="failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Graph execution failed: {exc}") from exc

    return RunCreatedResponse(
        run_id=run_id,
        status="awaiting_approval",
        message=(
            f"De-provisioning pack generated for {form.display_name}. "
            f"Awaiting human approval. POST /approve/{run_id} to proceed."
        ),
    )


@app.get(
    "/status/{run_id}",
    response_model=RunStatusResponse,
    summary="Poll run status",
    tags=["Status"],
)
def get_status(run_id: str) -> RunStatusResponse:
    """
    Poll the current status of an onboarding or offboarding run.
    """
    record = _run_registry.get(run_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

    mode = record.get("mode", "onboarding")
    graph_state = _get_graph_state(run_id, mode) or {}

    checklist = graph_state.get("checklist", [])
    scripts = graph_state.get("scripts", {})
    audit = graph_state.get("audit_report", {})
    approval_status = graph_state.get("approval_status") or record.get("approval_status")

    # Determine status
    status = record.get("status", "running")
    if audit and audit.get("passed") is not None:
        status = "complete"
        _update_run(run_id, status="complete", approval_status=approval_status)
    elif approval_status == "pending" or (not audit and not record.get("error")):
        status = "awaiting_approval"

    return RunStatusResponse(
        run_id=run_id,
        status=status,
        approval_status=approval_status,
        checklist_count=len(checklist) if checklist else None,
        scripts_generated=len(scripts) if scripts else None,
        audit_passed=audit.get("passed") if audit else None,
        error=record.get("error") or graph_state.get("error") or None,
        created_at=datetime.fromisoformat(record["created_at"]) if record.get("created_at") else None,
        updated_at=datetime.fromisoformat(record["updated_at"]) if record.get("updated_at") else None,
    )


@app.post(
    "/approve/{run_id}",
    response_model=RunStatusResponse,
    summary="Submit human approval or rejection",
    tags=["Approval"],
)
def submit_approval(run_id: str, approval: ApprovalRequest) -> RunStatusResponse:
    """
    Submit a human approval or rejection decision for a paused run.

    - `approved: true`  → graph continues to the AuditorAgent → completes
    - `approved: false` → graph routes back to PlannerAgent with reviewer notes
    """
    record = _run_registry.get(run_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

    mode = record.get("mode", "onboarding")
    graph = _onboarding_graph if mode == "onboarding" else _leaver_graph

    config = {"configurable": {"thread_id": run_id}}
    resume_value = {"approved": approval.approved, "notes": approval.notes}

    logger.info(
        "API: run %s approval=%s notes=%r",
        run_id, approval.approved, approval.notes,
    )

    try:
        # Resume the interrupted graph with the approval decision
        graph.invoke(Command(resume=resume_value), config=config)
        _update_run(
            run_id,
            status="complete" if approval.approved else "awaiting_approval",
            approval_status="approved" if approval.approved else "rejected",
        )
    except Exception as exc:
        logger.error("API: approval resumption failed for %s: %s", run_id, exc)
        _update_run(run_id, status="failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Graph resumption failed: {exc}") from exc

    return get_status(run_id)


@app.get(
    "/pack/{run_id}",
    summary="Download finalised pack as ZIP",
    tags=["Pack"],
)
def download_pack(run_id: str) -> StreamingResponse:
    """
    Download the finalised onboarding/offboarding pack as a ZIP archive.

    The ZIP contains:
    - checklist.json
    - audit_report.json
    - welcome_email.html / .txt
    - day1_guide.md (or handover_notes.md for leavers)
    - scripts/ — rendered PowerShell scripts
    - metadata.json
    """
    record = _run_registry.get(run_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

    mode = record.get("mode", "onboarding")
    graph_state = _get_graph_state(run_id, mode)

    if not graph_state:
        raise HTTPException(
            status_code=409,
            detail="Graph state not yet available. The run may still be in progress.",
        )

    audit = graph_state.get("audit_report", {})
    if not audit:
        raise HTTPException(
            status_code=409,
            detail="Audit not yet complete. Approve the pack first via POST /approve/{run_id}.",
        )

    zip_bytes = _build_zip(run_id, graph_state)
    filename = f"onboarding_pack_{run_id}.zip"

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/pack/{run_id}/summary",
    response_model=PackSummaryResponse,
    summary="Get pack summary (JSON)",
    tags=["Pack"],
)
def get_pack_summary(run_id: str) -> PackSummaryResponse:
    """Return the full pack contents as a structured JSON response."""
    record = _run_registry.get(run_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

    mode = record.get("mode", "onboarding")
    graph_state = _get_graph_state(run_id, mode) or {}

    return PackSummaryResponse(
        run_id=run_id,
        form=graph_state.get("form", {}),
        checklist=graph_state.get("checklist", []),
        scripts=graph_state.get("scripts", {}),
        comms=graph_state.get("comms", {}),
        audit_report=graph_state.get("audit_report", {}),
        download_url=f"/pack/{run_id}",
    )


@app.get("/health", tags=["Health"])
def health_check() -> dict:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "graphs": {
            "onboarding": _onboarding_graph is not None,
            "leaver": _leaver_graph is not None,
        },
        "active_runs": len(_run_registry),
    }
