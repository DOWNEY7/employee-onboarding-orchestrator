"""
core/observability.py
─────────────────────────────────────────────────────────────────────────────
Observability, Telemetry, and Prometheus Metrics engine.

Provides:
  - Request ID & W3C Trace ID ContextVars for logging correlation
  - Prometheus metrics definitions for API latency, traffic, active runs, LLM calls
  - Response generator for /metrics endpoint
"""

from __future__ import annotations

import contextvars
from typing import Any

from fastapi import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ── ContextVars for Request Correlation ───────────────────────────────────────

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
trace_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def set_request_context(request_id: str, trace_id: str) -> None:
    """Set the request_id and trace_id contextvars for the current task context."""
    request_id_ctx.set(request_id)
    trace_id_ctx.set(trace_id)


def get_request_context() -> dict[str, str]:
    """Retrieve current request_id and trace_id context."""
    return {
        "request_id": request_id_ctx.get(),
        "trace_id": trace_id_ctx.get(),
    }


# ── Prometheus Metrics Registry ───────────────────────────────────────────────

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total count of HTTP requests served by the orchestrator API",
    ["method", "endpoint", "status_code"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency histogram in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

ACTIVE_WORKFLOW_RUNS = Gauge(
    "active_workflow_runs",
    "Number of active onboarding and offboarding workflow runs",
    ["mode"],
)

WORKFLOW_RUNS_TOTAL = Counter(
    "workflow_runs_total",
    "Total number of workflow runs created or completed",
    ["mode", "status"],
)

GRAPH_EXECUTION_DURATION_SECONDS = Histogram(
    "graph_execution_duration_seconds",
    "LangGraph workflow execution duration in seconds",
    ["mode"],
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

LLM_CALLS_TOTAL = Counter(
    "llm_calls_total",
    "Total LLM calls executed across agent nodes",
    ["provider", "status"],
)


def get_prometheus_metrics_response() -> Response:
    """Render and return current Prometheus metrics as HTTP response."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
