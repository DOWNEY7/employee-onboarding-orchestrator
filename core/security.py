"""
core/security.py
─────────────────────────────────────────────────────────────────────────────
OAuth 2.0 & JWT Security Layer for Employee Onboarding Orchestrator.

Provides:
  - Salted PBKDF2 password hashing & verification (standard library hashlib)
  - User model & role definitions (admin, hr, approver, auditor, read_only)
  - User store loaded from settings or environment
  - OAuth2 JWT token creation, decoding, and validation
  - Unified authentication dependency (supports JWT and legacy API_KEY)
  - Role-Based Access Control (RBAC) dependency helper
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer
from pydantic import BaseModel

from core.config import get_settings

# ── Roles ─────────────────────────────────────────────────────────────────────

ROLE_ADMIN = "admin"
ROLE_HR = "hr"
ROLE_APPROVER = "approver"
ROLE_AUDITOR = "auditor"
ROLE_READ_ONLY = "read_only"

VALID_ROLES = {ROLE_ADMIN, ROLE_HR, ROLE_APPROVER, ROLE_AUDITOR, ROLE_READ_ONLY}


# ── Models ────────────────────────────────────────────────────────────────────


class User(BaseModel):
    """User account details for OAuth2 authentication and RBAC."""

    username: str
    hashed_password: str
    role: str = ROLE_ADMIN
    disabled: bool = False


class TokenResponse(BaseModel):
    """OAuth2 token response schema."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    username: str


class TokenData(BaseModel):
    """Decoded JWT payload structure."""

    sub: str  # username
    role: str = ROLE_ADMIN
    exp: datetime


# ── Password Hashing (Salted PBKDF2-HMAC-SHA256) ─────────────────────────────


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with 100,000 iterations."""
    if salt is None:
        salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return f"{salt.hex()}:{key.hex()}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify plain password against salted PBKDF2 hash."""
    try:
        salt_hex, key_hex = hashed_password.split(":")
        salt = bytes.fromhex(salt_hex)
        expected_key = bytes.fromhex(key_hex)
        derived_key = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, 100_000)
        return hmac.compare_digest(derived_key, expected_key)
    except Exception:
        return False


# ── User Store ────────────────────────────────────────────────────────────────

_user_store: dict[str, User] = {}


def _init_user_store() -> dict[str, User]:
    """Initialize in-memory user store from settings or default users."""
    global _user_store
    settings = get_settings()
    store: dict[str, User] = {}

    if settings.auth_users_json:
        try:
            raw_users = json.loads(settings.auth_users_json)
            for uname, udata in raw_users.items():
                pwd = udata.get("password", "")
                role = udata.get("role", ROLE_ADMIN)
                h_pwd = udata.get("hashed_password") or hash_password(pwd)
                store[uname] = User(
                    username=uname,
                    hashed_password=h_pwd,
                    role=role if role in VALID_ROLES else ROLE_ADMIN,
                    disabled=udata.get("disabled", False),
                )
        except Exception:
            pass

    # Default fallback accounts if none supplied via JSON
    if not store:
        store["admin"] = User(
            username="admin",
            hashed_password=hash_password("admin_secret"),
            role=ROLE_ADMIN,
        )
        store["hr_user"] = User(
            username="hr_user",
            hashed_password=hash_password("hr_secret"),
            role=ROLE_HR,
        )
        store["approver_user"] = User(
            username="approver_user",
            hashed_password=hash_password("approver_secret"),
            role=ROLE_APPROVER,
        )

    _user_store = store
    return _user_store


def get_user(username: str) -> User | None:
    """Retrieve user by username from store."""
    if not _user_store:
        _init_user_store()
    return _user_store.get(username)


def authenticate_user(username: str, password: str) -> User | None:
    """Authenticate user credentials."""
    user = get_user(username)
    if not user or user.disabled:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


# ── JWT Operations ────────────────────────────────────────────────────────────


def create_access_token(
    subject: str,
    role: str = ROLE_ADMIN,
    expires_delta: timedelta | None = None,
) -> str:
    """Generate a signed JWT access token for a subject and role."""
    settings = get_settings()
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)

    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "exp": expire,
        "iat": datetime.now(UTC),
        "iss": "employee-onboarding-orchestrator",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> TokenData | None:
    """Decode and validate a JWT access token."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer="employee-onboarding-orchestrator",
        )
        username: str = payload.get("sub", "")
        role: str = payload.get("role", ROLE_ADMIN)
        exp_ts = payload.get("exp")
        if not username or exp_ts is None:
            return None
        exp_dt = datetime.fromtimestamp(exp_ts, tz=UTC)
        return TokenData(sub=username, role=role, exp=exp_dt)
    except (jwt.PyJWTError, ValueError):
        return None


# ── Authentication Scheme & Dependencies ──────────────────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token", auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> User | None:
    """
    Unified authentication dependency.
    Validates either:
      1. Bearer token matching API_KEY setting (legacy support, grants admin role)
      2. Valid signed JWT access token
    If neither API_KEY nor auth is configured in dev mode, permits unauthenticated dev access.
    """
    settings = get_settings()

    # Dev mode: API_KEY empty and no auth enforced
    if not settings.api_key and settings.jwt_secret_key == "dev-jwt-secret-key-change-in-production-32bytes!":
        if credentials is None:
            # Allow dev mode guest user
            return User(username="dev_user", hashed_password="", role=ROLE_ADMIN)

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header. Provide 'Authorization: Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_token = credentials.credentials

    # Check 1: Match configured static API key
    if settings.api_key and hmac.compare_digest(raw_token, settings.api_key):
        return User(username="api_key_user", hashed_password="", role=ROLE_ADMIN)

    # Check 2: Try JWT decoding
    token_data = decode_access_token(raw_token)
    if token_data:
        return User(username=token_data.sub, hashed_password="", role=token_data.role)

    # Authentication failed
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


class RequireRole:
    """Dependency callable to enforce specific user roles on endpoints."""

    def __init__(self, allowed_roles: list[str]) -> None:
        self.allowed_roles = set(allowed_roles)
        # Admin always inherits all permissions
        self.allowed_roles.add(ROLE_ADMIN)

    def __call__(self, user: User | None = Depends(get_current_user)) -> User:
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required.",
            )
        if user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' lacks permission for this operation. Required: {list(self.allowed_roles)}",
            )
        return user
