"""
api/middleware.py
─────────────────────────────────────────────────────────────────────────────
FastAPI Middlewares for Security, Rate Limiting, and Observability Tracing.

Provides:
  - RequestTracingMiddleware: Correlation ID & W3C Trace context propagation,
    Prometheus latency & traffic counter recording.
  - SecurityHeadersMiddleware: Production HTTP security header enforcement.
  - RateLimitingMiddleware: Client IP sliding-window rate limiting with 429 response.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.config import get_settings
from core.observability import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    set_request_context,
)


class RequestTracingMiddleware(BaseHTTPMiddleware):
    """
    Middleware to ingest/generate correlation IDs and trace context,
    set logging contextvars, and record Prometheus traffic metrics.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Ingest or generate request_id
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        # Ingest or generate W3C trace_id
        traceparent = request.headers.get("traceparent")
        if traceparent and len(traceparent.split("-")) >= 2:
            trace_id = traceparent.split("-")[1]
        else:
            trace_id = request.headers.get("X-Trace-ID") or uuid.uuid4().hex

        set_request_context(request_id=request_id, trace_id=trace_id)

        start_time = time.perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed = time.perf_counter() - start_time
            status_code = response.status_code if response else 500
            endpoint = request.url.path

            # Normalise high-cardinality dynamic paths (e.g. /status/UUID -> /status/{id})
            norm_endpoint = endpoint
            if "/status/" in endpoint:
                norm_endpoint = "/status/{run_id}"
            elif "/approve/" in endpoint:
                norm_endpoint = "/approve/{run_id}"
            elif "/pack/" in endpoint:
                norm_endpoint = "/pack/{run_id}"

            HTTP_REQUESTS_TOTAL.labels(
                method=request.method,
                endpoint=norm_endpoint,
                status_code=str(status_code),
            ).inc()

            HTTP_REQUEST_DURATION_SECONDS.labels(
                method=request.method,
                endpoint=norm_endpoint,
            ).observe(elapsed)

            if response is not None:
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Trace-ID"] = trace_id


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware enforcing OWASP production HTTP security headers."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)
        settings = get_settings()

        if settings.enable_security_headers:
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-XSS-Protection"] = "1; mode=block"
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; frame-ancestors 'none';"
            )
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        return response


class RateLimitingMiddleware(BaseHTTPMiddleware):
    """
    Sliding window rate limiter per client IP.
    Returns HTTP 429 Too Many Requests when configured threshold is exceeded.
    """

    def __init__(self, app: Callable) -> None:
        super().__init__(app)
        self._requests: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Exempt health and metrics endpoints from rate limiting
        if request.url.path in ("/health", "/health/liveness", "/health/readiness", "/metrics"):
            return await call_next(request)

        settings = get_settings()
        limit = settings.rate_limit_per_minute
        if limit <= 0:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        window_start = now - 60.0

        # Prune requests older than 1 minute
        history = [ts for ts in self._requests[client_ip] if ts > window_start]
        self._requests[client_ip] = history

        if len(history) >= limit:
            retry_after = int(60.0 - (now - history[0])) if history else 60
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": "Rate limit exceeded. Please retry later.",
                    "limit": limit,
                    "retry_after_seconds": max(1, retry_after),
                },
                headers={
                    "Retry-After": str(max(1, retry_after)),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        self._requests[client_ip].append(now)
        response: Response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(limit - len(self._requests[client_ip]))
        return response
