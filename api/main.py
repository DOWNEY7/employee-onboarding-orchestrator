"""
api/main.py
─────────────────────────────────────────────────────────────────────────────
FastAPI application for the Employee Onboarding Orchestrator.

Endpoints:
  POST /onboard             — Start an onboarding workflow
  POST /offboard            — Start a leaver/offboarding workflow
  GET  /status/{run_id}     — Poll run status
  POST /approve/{run_id}    — Submit human approval/rejection decision
  GET  /pack/{run_id}       — Download finalised pack as ZIP
  GET  /pack/{run_id}/summary — Pack contents as JSON
  GET  /health              — Health check (public — no auth required)

Architecture:
  - All config is driven by core/config.py (pydantic-settings, reads .env)
  - LangGraph graphs are compiled once at startup (process-scoped singleton)
  - SQLite checkpointer persists graph state between API calls
  - RunStore (api/run_store.py) persists run metadata in the same SQLite DB,
    replacing the old ephemeral in-memory dict — survives server restarts
  - All endpoints except /health require a Bearer token (API_KEY env var)
    If API_KEY is not set the server runs in dev mode with no auth enforced
  - Structured JSON log lines are emitted to stdout
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Response, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordRequestForm
from langgraph.types import Command

from api.middleware import (
    RateLimitingMiddleware,
    RequestTracingMiddleware,
    SecurityHeadersMiddleware,
)
from api.run_store import RunStore
from core.config import get_settings
from core.observability import (
    WORKFLOW_RUNS_TOTAL,
    get_prometheus_metrics_response,
    get_request_context,
)
from core.security import (
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_AUDITOR,
    ROLE_HR,
    RequireRole,
    TokenResponse,
    User,
    authenticate_user,
    create_access_token,
    get_current_user,
)
from graph.leaver_graph import build_leaver_graph
from graph.onboarding_graph import build_onboarding_graph
from models.schemas import (
    ApprovalRequest,
    LeaverForm,
    PackSummaryResponse,
    RunCreatedResponse,
    RunStatusResponse,
    StarterForm,
)

# ── Structured JSON logging ───────────────────────────────────────────────────


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Inject correlation trace IDs from contextvars
        ctx = get_request_context()
        if ctx.get("request_id"):
            payload["request_id"] = ctx["request_id"]
        if ctx.get("trace_id"):
            payload["trace_id"] = ctx["trace_id"]
        # Forward any contextual fields attached to the record
        for extra_key in ("run_id", "mode"):
            val = getattr(record, extra_key, None)
            if val is not None:
                payload[extra_key] = val
        return json.dumps(payload, default=str)


def _configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logging.root.setLevel(level)
    logging.root.handlers = [handler]


# ── Auth & RBAC Dependencies ──────────────────────────────────────────────────

_auth_any = Depends(get_current_user)
_auth_hr = Depends(RequireRole([ROLE_HR]))
_auth_approver = Depends(RequireRole([ROLE_APPROVER]))
_auth_auditor = Depends(RequireRole([ROLE_AUDITOR]))



# ── Process-scoped singletons ─────────────────────────────────────────────────

_onboarding_graph = None
_leaver_graph = None
_run_store: RunStore | None = None

logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build graphs and run store on startup; tear down on shutdown."""
    global _onboarding_graph, _leaver_graph, _run_store

    settings = get_settings()
    _configure_logging(settings.log_level)

    if not settings.api_key:
        logger.warning(
            "API_KEY is not configured — the API is running unauthenticated. "
            "Set API_KEY in .env before exposing this service externally."
        )

    logger.info("API: initialising (db=%s)", settings.sqlite_path)

    # Shared SQLite connection for the run metadata store
    conn = sqlite3.connect(settings.sqlite_path, check_same_thread=False)
    _run_store = RunStore(conn)

    # Each graph builder opens its own connection to the same DB file
    logger.info("API: building LangGraph graphs...")
    _onboarding_graph, _ = build_onboarding_graph(db_path=settings.sqlite_path)
    _leaver_graph, _ = build_leaver_graph(db_path=settings.sqlite_path)

    logger.info("API: graphs ready — onboarding and leaver workflows compiled")

    yield  # ← app is running

    logger.info("API: shutting down")
    conn.close()


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

import os as _os

_cors_raw = _os.environ.get("CORS_ORIGINS", "*")
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]

# Middleware registration (Order: outer to inner: SecurityHeaders -> RateLimiting -> RequestTracing -> CORS)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitingMiddleware)
app.add_middleware(RequestTracingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "traceparent"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _new_run_id() -> str:
    return str(uuid.uuid4())


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

        # Core artefacts
        zf.writestr(
            f"{run_id}/checklist.json",
            json.dumps(checklist, indent=2, default=str),
        )
        zf.writestr(
            f"{run_id}/audit_report.json",
            json.dumps(audit, indent=2, default=str),
        )

        # Communications artefacts
        if comms.get("welcome_email_html"):
            zf.writestr(f"{run_id}/welcome_email.html", comms["welcome_email_html"])
        if comms.get("welcome_email_plain"):
            zf.writestr(f"{run_id}/welcome_email.txt", comms["welcome_email_plain"])
        if comms.get("day1_guide_markdown"):
            zf.writestr(f"{run_id}/day1_guide.md", comms["day1_guide_markdown"])

        # PowerShell scripts — ARTIFACT ONLY, never executed
        for script_name, content in scripts.items():
            zf.writestr(f"{run_id}/scripts/{script_name}", content)

        # Metadata
        meta = {
            "run_id": run_id,
            "form": form,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        zf.writestr(f"{run_id}/metadata.json", json.dumps(meta, indent=2, default=str))

    buf.seek(0)
    return buf.read()


# ── Routes ────────────────────────────────────────────────────────────────────


@app.post(
    "/token",
    response_model=TokenResponse,
    summary="OAuth2 Token Login Endpoint",
    tags=["Authentication"],
)
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()) -> TokenResponse:
    """
    OAuth2 Password Bearer flow endpoint.
    Accepts username & password (form data or URL encoded), returns signed JWT token.
    Default credentials for dev: admin / admin_secret, hr_user / hr_secret, approver_user / approver_secret.
    """
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    settings = get_settings()
    access_token = create_access_token(subject=user.username, role=user.role)
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        role=user.role,
        username=user.username,
    )


@app.get("/metrics", summary="Prometheus Metrics Endpoint", tags=["Observability"])
def prometheus_metrics() -> Response:
    """Export application and workflow metrics for Prometheus scraping."""
    return get_prometheus_metrics_response()


@app.post(
    "/onboard",
    response_model=RunCreatedResponse,
    status_code=202,
    summary="Start an onboarding workflow",
    tags=["Onboarding"],
    dependencies=[_auth_hr],
)
def start_onboarding(form: StarterForm) -> RunCreatedResponse:
    """
    Accept a new-starter form and start the onboarding workflow.

    The graph runs until the human approval gate, then pauses.
    Poll `GET /status/{run_id}` to check state.
    Submit approval via `POST /approve/{run_id}`.
    """
    run_id = _new_run_id()
    assert _run_store is not None
    _run_store.create(run_id, "onboarding", form.model_dump(mode="json"))
    WORKFLOW_RUNS_TOTAL.labels(mode="onboarding", status="started").inc()

    config = {"configurable": {"thread_id": run_id}}
    initial_state: dict[str, Any] = {
        "run_id": run_id,
        "form": form.model_dump(mode="json"),
        "iteration": 0,
        "error": "",
    }

    logger.info("API: starting onboarding run %s for %s", run_id, form.display_name)

    try:
        _onboarding_graph.invoke(initial_state, config=config)
        _run_store.update(run_id, status="awaiting_approval")
        logger.info("API: run %s paused at human_gate", run_id)
    except Exception as exc:
        logger.exception("API: onboarding run %s failed", run_id)
        _run_store.update(run_id, status="failed", error=str(exc))
        WORKFLOW_RUNS_TOTAL.labels(mode="onboarding", status="failed").inc()
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
    dependencies=[_auth_hr],
)
def start_offboarding(form: LeaverForm) -> RunCreatedResponse:
    """Accept a leaver form and start the de-provisioning workflow."""
    run_id = _new_run_id()
    assert _run_store is not None
    _run_store.create(run_id, "leaver", form.model_dump(mode="json"))

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
        _run_store.update(run_id, status="awaiting_approval")
    except Exception as exc:
        logger.exception("API: leaver run %s failed", run_id)
        _run_store.update(run_id, status="failed", error=str(exc))
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
    dependencies=[_auth_any],
)
def get_status(run_id: str) -> RunStatusResponse:
    """Poll the current status of an onboarding or offboarding run."""
    assert _run_store is not None
    record = _run_store.get(run_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

    mode = record.get("mode", "onboarding")
    graph_state = _get_graph_state(run_id, mode) or {}

    checklist = graph_state.get("checklist", [])
    scripts = graph_state.get("scripts", {})
    audit = graph_state.get("audit_report", {})
    approval_status = graph_state.get("approval_status") or record.get("approval_status")

    # Sync store status with graph state
    run_status = record.get("status", "running")
    if audit and audit.get("passed") is not None:
        run_status = "complete"
        _run_store.update(run_id, status="complete", approval_status=approval_status)
    elif approval_status == "pending" or (not audit and not record.get("error")):
        run_status = "awaiting_approval"

    return RunStatusResponse(
        run_id=run_id,
        status=run_status,
        approval_status=approval_status,
        checklist_count=len(checklist) if checklist else None,
        scripts_generated=len(scripts) if scripts else None,
        audit_passed=audit.get("passed") if audit else None,
        error=record.get("error") or graph_state.get("error") or None,
        created_at=(
            datetime.fromisoformat(record["created_at"]) if record.get("created_at") else None
        ),
        updated_at=(
            datetime.fromisoformat(record["updated_at"]) if record.get("updated_at") else None
        ),
    )


@app.post(
    "/approve/{run_id}",
    response_model=RunStatusResponse,
    summary="Submit human approval or rejection",
    tags=["Approval"],
    dependencies=[_auth_approver],
)
def submit_approval(run_id: str, approval: ApprovalRequest) -> RunStatusResponse:
    """
    Submit a human approval or rejection decision for a paused run.

    - ``approved: true``  → graph continues to the AuditorAgent → completes
    - ``approved: false`` → graph routes back to PlannerAgent with reviewer notes
    """
    assert _run_store is not None
    record = _run_store.get(run_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

    mode = record.get("mode", "onboarding")
    graph = _onboarding_graph if mode == "onboarding" else _leaver_graph

    config = {"configurable": {"thread_id": run_id}}
    resume_value = {"approved": approval.approved, "notes": approval.notes}

    logger.info(
        "API: run %s approval=%s notes=%r",
        run_id,
        approval.approved,
        approval.notes,
    )

    try:
        graph.invoke(Command(resume=resume_value), config=config)
        _run_store.update(
            run_id,
            status="complete" if approval.approved else "awaiting_approval",
            approval_status="approved" if approval.approved else "rejected",
        )
    except Exception as exc:
        logger.exception("API: approval resumption failed for %s", run_id)
        _run_store.update(run_id, status="failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Graph resumption failed: {exc}") from exc

    return get_status(run_id)


@app.get(
    "/pack/{run_id}",
    summary="Download finalised pack as ZIP",
    tags=["Pack"],
    dependencies=[_auth_auditor],
)
def download_pack(run_id: str) -> StreamingResponse:
    """
    Download the finalised onboarding/offboarding pack as a ZIP archive.

    The ZIP contains:
    - checklist.json
    - audit_report.json
    - welcome_email.html / .txt
    - day1_guide.md (or handover_notes.md for leavers)
    - scripts/ — PowerShell scripts (ARTIFACT ONLY — NOT EXECUTED)
    - metadata.json
    """
    assert _run_store is not None
    record = _run_store.get(run_id)
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
            detail=(f"Audit not yet complete. Approve the pack first via POST /approve/{run_id}."),
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
    dependencies=[_auth_auditor],
)
def get_pack_summary(run_id: str) -> PackSummaryResponse:
    """Return the full pack contents as a structured JSON response."""
    assert _run_store is not None
    record = _run_store.get(run_id)
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


@app.get("/health/liveness", summary="Kubernetes Liveness Probe", tags=["Health"])
def liveness_probe() -> dict[str, str]:
    """Liveness probe — confirms API process is responsive."""
    return {"status": "alive"}


@app.get("/health/readiness", summary="Kubernetes Readiness Probe", tags=["Health"])
def readiness_probe() -> dict[str, Any]:
    """Readiness probe — verifies DB connectivity and graph compilation."""
    db_writable = False
    try:
        if _run_store is not None:
            _run_store._conn.execute("SELECT 1")
            db_writable = True
    except Exception:
        pass

    ready = _onboarding_graph is not None and _leaver_graph is not None and db_writable
    if not ready:
        raise HTTPException(
            status_code=status.HTTP_533_SERVICE_UNAVAILABLE if hasattr(status, "HTTP_533_SERVICE_UNAVAILABLE") else 503,
            detail="Service not ready",
        )

    return {
        "status": "ready",
        "db_writable": db_writable,
        "graphs": {
            "onboarding": _onboarding_graph is not None,
            "leaver": _leaver_graph is not None,
        },
    }


@app.get("/health", tags=["Health"])
def health_check() -> dict:
    """
    Health check endpoint — public, no auth required.

    Checks that graphs are compiled and the SQLite database is writable.
    Suitable for Kubernetes liveness/readiness probes and Docker HEALTHCHECK.
    """
    db_writable = False
    try:
        if _run_store is not None:
            _run_store._conn.execute("SELECT 1")
            db_writable = True
    except Exception:
        pass

    overall_healthy = _onboarding_graph is not None and db_writable

    return {
        "status": "healthy" if overall_healthy else "degraded",
        "version": "0.1.0",
        "graphs": {
            "onboarding": _onboarding_graph is not None,
            "leaver": _leaver_graph is not None,
        },
        "db_writable": db_writable,
        "active_runs": len(_run_store.list_all()) if _run_store is not None else 0,
    }
