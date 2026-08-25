"""
tests/test_observability.py
─────────────────────────────────────────────────────────────────────────────
Unit and Integration tests for Prometheus metrics, correlation tracing,
and Kubernetes health probes.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core.config import get_settings


@pytest.fixture()
def _mocks():
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
    og_mock, lg_mock = _mocks
    db_path = str(tmp_path / "test_obs.db")

    get_settings.cache_clear()
    with (
        patch("graph.onboarding_graph.build_onboarding_graph", return_value=(og_mock, None)),
        patch("graph.leaver_graph.build_leaver_graph", return_value=(lg_mock, None)),
        patch.object(get_settings(), "database_url", f"sqlite:///{db_path}"),
        TestClient(app) as test_client,
    ):
        yield test_client


class TestPrometheusMetrics:
    def test_metrics_endpoint_returns_prometheus_format(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        body = response.text
        assert "http_requests_total" in body
        assert "http_request_duration_seconds" in body
        assert "workflow_runs_total" in body

    def test_request_increments_http_metrics_counter(self, client):
        client.get("/health")
        metrics_resp = client.get("/metrics")
        assert metrics_resp.status_code == 200
        assert 'http_requests_total{endpoint="/health"' in metrics_resp.text


class TestRequestTracingContext:
    def test_x_request_id_header_propagated(self, client):
        custom_req_id = "test-req-id-12345"
        response = client.get("/health", headers={"X-Request-ID": custom_req_id})
        assert response.status_code == 200
        assert response.headers.get("X-Request-ID") == custom_req_id

    def test_traceparent_header_extracted(self, client):
        traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        response = client.get("/health", headers={"traceparent": traceparent})
        assert response.status_code == 200
        assert response.headers.get("X-Trace-ID") == "4bf92f3577b34da6a3ce929d0e0e4736"


class TestKubernetesHealthProbes:
    def test_liveness_probe(self, client):
        response = client.get("/health/liveness")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    def test_readiness_probe(self, client):
        response = client.get("/health/readiness")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["db_writable"] is True
        assert data["graphs"]["onboarding"] is True
        assert data["graphs"]["leaver"] is True
