# ── Stage: build ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS build

WORKDIR /app

# Install dependencies separately for layer caching
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

# ── Stage: runtime ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Copy installed packages from build stage
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin /usr/local/bin

# Copy application source
COPY agents/ agents/
COPY api/ api/
COPY checklists/ checklists/
COPY core/ core/
COPY graph/ graph/
COPY models/ models/
COPY scripts/ scripts/
COPY pyproject.toml .

# Data directory for SQLite persistence (mount a volume here in production)
RUN mkdir -p /app/data && chown appuser:appuser /app/data

USER appuser

# Expose the FastAPI port
EXPOSE 8000

# Health check — matches the public /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
    || exit 1

# DATABASE_URL, API_KEY, LLM_PROVIDER, and credentials should be supplied
# via environment variables or a mounted .env file at runtime.
ENV DATABASE_URL="sqlite:////app/data/onboarding.db" \
    LOG_LEVEL="INFO"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
