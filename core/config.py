"""
core/config.py
─────────────────────────────────────────────────────────────────────────────
Application settings loaded from environment variables (or a .env file).
Uses pydantic-settings so every field is type-checked and validated at
startup. A missing required value raises a clear validation error.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Placeholder JWT secret shipped in this public repo. Anyone can read it on
# GitHub, so a token signed with this value must never be trusted once auth
# has actually been enabled (enforced in core/security.py).
DEFAULT_JWT_SECRET_KEY = "dev-jwt-secret-key-change-in-production-32bytes!"


class Settings(BaseSettings):
    """All environment-driven configuration for the orchestrator."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM provider ──────────────────────────────────────────────────────────
    llm_provider: str = Field(
        default="azure",
        description="'azure' (Azure OpenAI) or 'openai' (OpenAI direct)",
    )

    # Azure OpenAI — required when llm_provider == "azure"
    azure_openai_endpoint: str = Field(default="")
    azure_openai_api_key: str = Field(default="")
    azure_openai_deployment: str = Field(default="gpt-4o")
    azure_openai_api_version: str = Field(default="2024-08-01-preview")

    # OpenAI direct — required when llm_provider == "openai"
    openai_api_key: str = Field(default="")
    openai_model: str = Field(default="gpt-4o")

    # ── App ───────────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO")
    database_url: str = Field(
        default="sqlite:///./onboarding.db",
        description="SQLite path for LangGraph checkpointer",
    )
    pack_output_dir: Path = Field(
        default=Path("./output_packs"),
        description="Directory where finalised ZIP packs are written",
    )
    domain: str = Field(
        default="contoso.com",
        description="Default tenant domain for generated UPNs",
    )

    # ── Security ──────────────────────────────────────────────────────────────
    api_key: str = Field(
        default="",
        description=(
            "Bearer token required on all API calls. "
            "If empty, the server starts in dev mode with no auth enforced. "
            "Generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        ),
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: [os.environ.get("CORS_ORIGINS", "*")],
        description="Allowed CORS origins. Set to your frontend domain in production.",
    )
    jwt_secret_key: str = Field(
        default=DEFAULT_JWT_SECRET_KEY,
        description="Secret key for JWT signature validation",
    )
    jwt_algorithm: str = Field(
        default="HS256",
        description="Algorithm for signing JWT access tokens",
    )
    access_token_expire_minutes: int = Field(
        default=60,
        description="Expiration time for OAuth2 access tokens in minutes",
    )
    rate_limit_per_minute: int = Field(
        default=120,
        description="Maximum allowed requests per minute per IP",
    )
    enable_security_headers: bool = Field(
        default=True,
        description="Whether to enforce security headers middleware",
    )
    auth_users_json: str = Field(
        default="",
        description="JSON object mapping username to {password, role} for user auth store",
    )

    # ── Derived helpers ───────────────────────────────────────────────────────
    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return upper

    @property
    def sqlite_path(self) -> str:
        """Extract the file path from the sqlite:// URL for langgraph."""
        return self.database_url.replace("sqlite:///", "")

    def configure_logging(self) -> None:
        logging.basicConfig(
            level=getattr(logging, self.log_level),
            format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    settings = Settings()
    settings.pack_output_dir.mkdir(parents=True, exist_ok=True)
    return settings
