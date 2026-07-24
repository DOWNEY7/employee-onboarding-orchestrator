"""
tests/test_api.py
─────────────────────────────────────────────────────────────────────────────
Integration tests for the FastAPI layer.

These tests cover:
  - /health (public, no auth)
  - Auth enforcement on all other endpoints
  - Full onboarding flow: POST /onboard → GET /status → POST /approve → GET /pack
  - 409 when requesting pack before approval
  - Full offboarding flow: POST /offboard
  - 404 for unknown run IDs

All LangGraph graph invocations are mocked so no LLM or pwsh is required.
A temporary SQLite database is created per test for RunStore isolation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app

# ── Constants ─────────────────────────────────────────────────────────────────

TEST_KEY = "test-ci-api-key-do-not-use-in-production"
AUTH = {"Authorization": f"Bearer {TEST_KEY}"}

STARTER_PAYLOAD: dict[str, Any] = {
    "first_name": "Jane",
    "last_name": "Smith",
    "role": "Software Engineer",
    "department": "Engineering",
    "start_date": "2025-09-01",
    "manager_email": "john.doe@contoso.com",
    "email_prefix": "jane.smith",
    "domain": "contoso.com",
    "licence_sku": "E3",
    "location": "UK",
}

LEAVER_PAYLOAD: dict[str, Any] = {
    "first_name": "John",
    "last_name": "Doe",
    "email": "john.doe@contoso.com",
    "last_working_day": "2025-09-30",
    "manager_email": "sarah.jones@contoso.com",
    "reason": "Resignation",
    "hardware_return_required": True,
}

# ── Graph state mocks ─────────────────────────────────────────────────────────


def _make_graph_state(with_audit: bool = True, error: str = "") -> MagicMock:
    """Return a fake LangGraph state snapshot."""
    state = MagicMock()
    state.values = {
        "form": {"first_name": "Jane", "last_name": "Smith", "role": "SWE"},
        "checklist": [
            {
                "id": "abc12345",
                "step_number": 1,
                "title": "Create M365 user account",
                "description": "Create the user in Azure AD.",
                "system": "Azure AD",
                "owner": "IT",
                "priority": "Critical",
                "tags": ["identity"],
                "completed": False,
                "notes": "",
            }
        ],
        "scripts": {"01_create_user.ps1": "# ARTIFACT ONLY — NOT EXECUTED\n# script body"},
        "comms": {
            "welcome_email_subject": "Welcome to the team, Jane!",
            "welcome_email_html": "<p>Welcome Jane!</p>",
            "welcome_email_plain": "Welcome Jane!",
            "day1_guide_markdown": "# Day 1 Guide\n\nHere's what to expect.",
        },
        "audit_report": (
            {
                "passed": True,
                "total_required": 1,
                "total_present": 1,
                "findings": [],
                "auditor_notes": "All good.",
            }
            if with_audit
            else {}
        ),
        "approval_status": "approved" if with_audit else "pending",
        "error": error,
    }
    return state


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def _mocks():
    """Return fresh mock graph objects for each test."""
    og = MagicMock()
    og.invoke.return_value = {}
    og.get_state.return_value = _make_graph_state(with_audit=True)

    lg = MagicMock()
    lg.invoke.return_value = {}
    lg.get_state.return_value = _make_graph_state(with_audit=True)

    return og, lg


@pytest.fixture()
def client(tmp_path, _mocks):
    """
    TestClient with:
    - A temporary SQLite database (RunStore isolation per test)
    - Mocked LangGraph graph builders (no LLM, no real graph execution)
    - API_KEY set to TEST_KEY
    """
    mock_og, mock_lg = _mocks
    db_path = str(tmp_path / "test_onboarding.db")

    from core.config import get_settings

    env = {
        "DATABASE_URL": f"sqlite:///{db_path}",
        "API_KEY": TEST_KEY,
        "LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-test-placeholder",
        "LOG_LEVEL": "WARNING",  # suppress noise in test output
    }

    get_settings.cache_clear()
    with (
        patch.dict("os.environ", env, clear=False),
        patch("api.main.build_onboarding_graph", return_value=(mock_og, MagicMock())),
        patch("api.main.build_leaver_graph", return_value=(mock_lg, MagicMock())),
    ):
        get_settings.cache_clear()
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c, mock_og, mock_lg

    get_settings.cache_clear()


# ── Health (public) ───────────────────────────────────────────────────────────


class TestHealth:
    def test_health_public_no_auth_required(self, client):
        c, *_ = client
        r = c.get("/health")
        assert r.status_code == 200

    def test_health_response_shape(self, client):
        c, *_ = client
        data = c.get("/health").json()
        assert "status" in data
        assert "version" in data
        assert "graphs" in data
        assert "db_writable" in data
        assert "active_runs" in data
        assert data["version"] == "0.1.0"
        assert data["graphs"]["onboarding"] is True
        assert data["graphs"]["leaver"] is True
        assert data["db_writable"] is True


# ── Auth enforcement ──────────────────────────────────────────────────────────


class TestAuthEnforcement:
    def test_onboard_without_key_returns_401(self, client):
        c, *_ = client
        r = c.post("/onboard", json=STARTER_PAYLOAD)
        assert r.status_code == 401

    def test_offboard_without_key_returns_401(self, client):
        c, *_ = client
        r = c.post("/offboard", json=LEAVER_PAYLOAD)
        assert r.status_code == 401

    def test_status_without_key_returns_401(self, client):
        c, *_ = client
        r = c.get("/status/some-run-id")
        assert r.status_code == 401

    def test_approve_without_key_returns_401(self, client):
        c, *_ = client
        r = c.post("/approve/some-run-id", json={"approved": True})
        assert r.status_code == 401

    def test_pack_without_key_returns_401(self, client):
        c, *_ = client
        r = c.get("/pack/some-run-id")
        assert r.status_code == 401

    def test_wrong_key_returns_401(self, client):
        c, *_ = client
        r = c.post("/onboard", json=STARTER_PAYLOAD, headers={"Authorization": "Bearer wrong-key"})
        assert r.status_code == 401


# ── Onboarding flow ───────────────────────────────────────────────────────────


class TestOnboardingFlow:
    def test_onboard_creates_run(self, client):
        c, mock_og, _ = client
        r = c.post("/onboard", json=STARTER_PAYLOAD, headers=AUTH)
        assert r.status_code == 202
        body = r.json()
        assert "run_id" in body
        assert body["status"] == "awaiting_approval"
        assert "approve" in body["message"]
        mock_og.invoke.assert_called_once()

    def test_status_not_found(self, client):
        c, *_ = client
        r = c.get("/status/00000000-does-not-exist", headers=AUTH)
        assert r.status_code == 404

    def test_status_returns_awaiting_approval(self, client):
        c, mock_og, _ = client
        # Configure mock to return state without audit (still awaiting approval)
        mock_og.get_state.return_value = _make_graph_state(with_audit=False)

        r = c.post("/onboard", json=STARTER_PAYLOAD, headers=AUTH)
        run_id = r.json()["run_id"]

        r = c.get(f"/status/{run_id}", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"] == run_id
        assert body["checklist_count"] == 1
        assert body["scripts_generated"] == 1

    def test_pack_before_approval_returns_409(self, client):
        c, mock_og, _ = client
        # State has no audit report yet
        mock_og.get_state.return_value = _make_graph_state(with_audit=False)

        r = c.post("/onboard", json=STARTER_PAYLOAD, headers=AUTH)
        run_id = r.json()["run_id"]

        r = c.get(f"/pack/{run_id}", headers=AUTH)
        assert r.status_code == 409

    def test_approve_run(self, client):
        c, mock_og, _ = client
        # After approval, get_state returns completed state
        mock_og.get_state.return_value = _make_graph_state(with_audit=True)

        r = c.post("/onboard", json=STARTER_PAYLOAD, headers=AUTH)
        run_id = r.json()["run_id"]

        r = c.post(
            f"/approve/{run_id}",
            json={"approved": True, "notes": "Pack looks correct."},
            headers=AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"] == run_id
        # invoke called twice: initial run + approval resume
        assert mock_og.invoke.call_count == 2

    def test_pack_download_after_approval(self, client):
        c, mock_og, _ = client
        mock_og.get_state.return_value = _make_graph_state(with_audit=True)

        r = c.post("/onboard", json=STARTER_PAYLOAD, headers=AUTH)
        run_id = r.json()["run_id"]

        c.post(f"/approve/{run_id}", json={"approved": True}, headers=AUTH)

        r = c.get(f"/pack/{run_id}", headers=AUTH)
        assert r.status_code == 200
        assert "application/zip" in r.headers["content-type"]
        # Verify it's a valid ZIP with expected files
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
        assert any("checklist.json" in n for n in names)
        assert any("audit_report.json" in n for n in names)
        assert any("scripts/" in n for n in names)

    def test_pack_summary_endpoint(self, client):
        c, mock_og, _ = client
        mock_og.get_state.return_value = _make_graph_state(with_audit=True)

        r = c.post("/onboard", json=STARTER_PAYLOAD, headers=AUTH)
        run_id = r.json()["run_id"]

        r = c.get(f"/pack/{run_id}/summary", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"] == run_id
        assert "checklist" in body
        assert "scripts" in body
        assert "download_url" in body


# ── Offboarding flow ──────────────────────────────────────────────────────────


class TestOffboardingFlow:
    def test_offboard_creates_run(self, client):
        c, _, mock_lg = client
        r = c.post("/offboard", json=LEAVER_PAYLOAD, headers=AUTH)
        assert r.status_code == 202
        body = r.json()
        assert "run_id" in body
        assert body["status"] == "awaiting_approval"
        mock_lg.invoke.assert_called_once()

    def test_offboard_run_status_tracked(self, client):
        c, _, mock_lg = client
        mock_lg.get_state.return_value = _make_graph_state(with_audit=False)

        r = c.post("/offboard", json=LEAVER_PAYLOAD, headers=AUTH)
        run_id = r.json()["run_id"]

        r = c.get(f"/status/{run_id}", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["run_id"] == run_id

    def test_approve_offboard_run(self, client):
        c, _, mock_lg = client
        mock_lg.get_state.return_value = _make_graph_state(with_audit=True)

        r = c.post("/offboard", json=LEAVER_PAYLOAD, headers=AUTH)
        run_id = r.json()["run_id"]

        r = c.post(
            f"/approve/{run_id}",
            json={"approved": True, "notes": "Leaver pack approved."},
            headers=AUTH,
        )
        assert r.status_code == 200
