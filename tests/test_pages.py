"""Smoke tests: the application boots and serves its public pages."""

import pytest
from conftest import JSON_HEADERS

pytestmark = pytest.mark.smoke


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert b"CloudPulse" in response.data


def test_register_page_renders(client):
    response = client.get("/register")
    assert response.status_code == 200
    assert b"CloudPulse" in response.data


def test_home_requires_login(client):
    response = client.get("/")
    assert response.status_code in (302, 401)


def test_home_renders_after_login(admin_client):
    response = admin_client.get("/")
    assert response.status_code == 200
    assert b"CloudPulse" in response.data
    assert b"Applications" in response.data


def test_health_endpoint_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "healthy"


def test_liveness_and_readiness(client):
    assert client.get("/health/live").status_code == 200
    readiness = client.get("/health/ready")
    assert readiness.status_code == 200
    assert readiness.get_json()["status"] == "ready"


def test_cicd_endpoint(client):
    response = client.get("/api/cicd")
    assert response.status_code == 200
    assert response.get_json()["status"] == "success"


def test_static_assets_are_served(client):
    for asset in (
        "style.css",
        "js/core.js",
        "js/charts.js",
        "js/overview.js",
        "js/applications.js",
        "js/incidents.js",
        "js/deployments.js",
        "js/activity.js",
        "js/main.js",
    ):
        assert client.get("/static/" + asset).status_code == 200, asset


def test_security_headers_present(client):
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_logout_clears_session(admin_client):
    assert admin_client.get("/logout").status_code == 302
    assert admin_client.get("/api/me", headers=JSON_HEADERS).status_code == 401


def test_unknown_api_route_returns_json(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert "error" in response.get_json()
