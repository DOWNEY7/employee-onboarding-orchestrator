"""
tests/test_security.py
─────────────────────────────────────────────────────────────────────────────
Unit and Integration tests for OAuth 2.0, JWT authentication, RBAC,
password hashing, Security Headers, and Rate Limiting.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core.config import DEFAULT_JWT_SECRET_KEY, get_settings
from core.security import (
    ROLE_ADMIN,
    ROLE_HR,
    ROLE_READ_ONLY,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


@pytest.fixture()
def _mocks():
    """Mock LangGraph graph objects."""
    state = MagicMock()
    state.values = {"form": {}, "checklist": [], "scripts": {}, "comms": {}, "audit_report": {}}
    og = MagicMock()
    og.invoke.return_value = {}
    og.get_state.return_value = state

    lg = MagicMock()
    lg.invoke.return_value = {}
    lg.get_state.return_value = state

    return og, lg


@pytest.fixture()
def client(tmp_path, _mocks):
    """TestClient running within lifespan context manager."""
    og_mock, lg_mock = _mocks
    db_path = str(tmp_path / "test_sec.db")

    get_settings.cache_clear()
    with (
        patch("graph.onboarding_graph.build_onboarding_graph", return_value=(og_mock, None)),
        patch("graph.leaver_graph.build_leaver_graph", return_value=(lg_mock, None)),
        patch.object(get_settings(), "database_url", f"sqlite:///{db_path}"),
        TestClient(app) as test_client,
    ):
        yield test_client


STARTER_PAYLOAD = {
    "first_name": "Alice",
    "last_name": "Smith",
    "role": "Software Engineer",
    "department": "Engineering",
    "start_date": "2026-08-01",
    "manager_email": "john.doe@contoso.com",
    "email_prefix": "alice.smith",
    "domain": "contoso.com",
    "licence_sku": "E3",
    "location": "UK",
}


# ── Password Hashing Tests ───────────────────────────────────────────────────


class TestPasswordHashing:
    def test_hash_and_verify_correct_password(self):
        pwd = "SecretPassword123!"
        hashed = hash_password(pwd)
        assert verify_password(pwd, hashed) is True

    def test_verify_wrong_password_fails(self):
        pwd = "SecretPassword123!"
        hashed = hash_password(pwd)
        assert verify_password("WrongPassword", hashed) is False

    def test_salt_is_unique(self):
        pwd = "SamePassword"
        h1 = hash_password(pwd)
        h2 = hash_password(pwd)
        assert h1 != h2
        assert verify_password(pwd, h1) is True
        assert verify_password(pwd, h2) is True


# ── JWT Token Tests ──────────────────────────────────────────────────────────


class TestJWTTokenOperations:
    def test_create_and_decode_valid_token(self):
        token = create_access_token(subject="admin", role=ROLE_ADMIN)
        token_data = decode_access_token(token)
        assert token_data is not None
        assert token_data.sub == "admin"
        assert token_data.role == ROLE_ADMIN

    def test_token_expiration(self):
        token = create_access_token(
            subject="expired_user",
            role=ROLE_HR,
            expires_delta=timedelta(seconds=-1),
        )
        token_data = decode_access_token(token)
        assert token_data is None

    def test_invalid_jwt_signature(self):
        token = create_access_token(subject="user", role=ROLE_ADMIN)
        tampered_token = token[:-5] + "XXXXX"
        token_data = decode_access_token(tampered_token)
        assert token_data is None


# ── OAuth2 Token Login Endpoint Tests ─────────────────────────────────────────


class TestOAuth2LoginEndpoint:
    def test_successful_login_returns_token(self, client):
        response = client.post(
            "/token",
            data={"username": "admin", "password": "admin_secret"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["role"] == ROLE_ADMIN
        assert data["username"] == "admin"

    def test_invalid_login_returns_401(self, client):
        response = client.post(
            "/token",
            data={"username": "admin", "password": "wrong_password"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Incorrect username or password."


# ── Role-Based Access Control (RBAC) Tests ────────────────────────────────────


class TestRBACPermissions:
    def test_hr_role_can_start_onboarding(self, client):
        hr_token = create_access_token(subject="hr_user", role=ROLE_HR)
        response = client.post(
            "/onboard",
            json=STARTER_PAYLOAD,
            headers={"Authorization": f"Bearer {hr_token}"},
        )
        assert response.status_code == 202
        assert "run_id" in response.json()

    def test_read_only_role_forbidden_from_onboarding(self, client):
        ro_token = create_access_token(subject="ro_user", role=ROLE_READ_ONLY)
        response = client.post(
            "/onboard",
            json=STARTER_PAYLOAD,
            headers={"Authorization": f"Bearer {ro_token}"},
        )
        assert response.status_code == 403
        assert "lacks permission" in response.json()["detail"]

    def test_admin_role_has_access_to_all_endpoints(self, client):
        admin_token = create_access_token(subject="admin", role=ROLE_ADMIN)
        response = client.get(
            "/status/non_existent_run",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert response.status_code == 404  # 404 means auth passed and endpoint executed


# ── Security Headers Tests ────────────────────────────────────────────────────


class TestSecurityHeaders:
    def test_security_headers_present_on_responses(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        headers = response.headers
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "DENY"
        assert headers.get("X-XSS-Protection") == "1; mode=block"
        assert "max-age=" in headers.get("Strict-Transport-Security", "")
        assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


# ── Public-Default JWT Secret Bypass Regression Test ──────────────────────────
#
# DEFAULT_JWT_SECRET_KEY is committed in this public repo, so anyone can sign
# a JWT with it. If an operator sets API_KEY (believing that locks the API
# down) but forgets to override JWT_SECRET_KEY, such a forged token must
# still be rejected — the static API_KEY must remain the only accepted
# credential until a real JWT secret is configured.


class TestPublicDefaultJwtSecretRejected:
    def test_forged_admin_jwt_rejected_when_api_key_set_but_jwt_secret_is_default(self, client):
        settings = get_settings()
        assert settings.jwt_secret_key == DEFAULT_JWT_SECRET_KEY  # sanity check

        with patch.object(settings, "api_key", "configured-test-api-key"):
            forged_token = create_access_token(subject="attacker", role=ROLE_ADMIN)
            response = client.get(
                "/status/whatever",
                headers={"Authorization": f"Bearer {forged_token}"},
            )
            assert response.status_code == 401

            # The static API key must still work.
            response = client.get(
                "/status/whatever",
                headers={"Authorization": "Bearer configured-test-api-key"},
            )
            assert response.status_code == 404  # auth passed, run not found


# ── Rate Limiting Tests ───────────────────────────────────────────────────────


class TestRateLimiting:
    def test_rate_limit_exceeded_returns_429(self, client):
        with patch.object(get_settings(), "rate_limit_per_minute", 2):
            token = create_access_token(subject="admin", role=ROLE_ADMIN)
            headers = {"Authorization": f"Bearer {token}"}

            # Clear any rate limit history for the client IP
            for middleware in app.user_middleware:
                if hasattr(middleware, "kwargs") and "cls" in middleware.kwargs:
                    pass
            # Perform requests until limit exceeded
            responses = []
            for i in range(5):
                responses.append(client.get(f"/status/test_{i}", headers=headers))

            statuses = [r.status_code for r in responses]
            assert 429 in statuses
