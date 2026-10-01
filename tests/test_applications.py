"""Application registry API tests."""

import pytest
from conftest import JSON_HEADERS

pytestmark = pytest.mark.integration

VALID_PAYLOAD = {
    "name": "payments-api",
    "url": "https://payments.example.com/health",
    "environment": "Production",
    "status": "Operational",
    "description": "Card payment service",
}


def create(client, **overrides):
    payload = dict(VALID_PAYLOAD)
    payload.update(overrides)
    return client.post("/api/applications", json=payload, headers=JSON_HEADERS)


def test_list_is_empty_initially(admin_client):
    response = admin_client.get("/api/applications", headers=JSON_HEADERS)
    assert response.status_code == 200
    assert response.get_json() == []


def test_create_application(admin_client):
    response = create(admin_client)
    assert response.status_code == 201

    application = response.get_json()["application"]
    assert application["name"] == "payments-api"
    assert application["environment"] == "Production"
    assert application["uptime_percentage"] == 100.0
    assert application["monitoring"]["timeout_seconds"] == 5


def test_create_requires_name_url_environment(admin_client):
    response = admin_client.post(
        "/api/applications", json={"name": "incomplete"}, headers=JSON_HEADERS
    )
    assert response.status_code == 422
    assert "url" in response.get_json()["error"]


def test_rejects_unsupported_url_scheme(admin_client):
    response = create(admin_client, url="ftp://example.com")
    assert response.status_code == 422
    assert "url" in response.get_json()["fields"]


def test_rejects_invalid_environment(admin_client):
    response = create(admin_client, environment="Sandbox")
    assert response.status_code == 422
    assert "environment" in response.get_json()["fields"]


def test_rejects_invalid_header_json(admin_client):
    response = create(admin_client, request_headers="not-json")
    assert response.status_code == 422
    assert "request_headers" in response.get_json()["fields"]


def test_get_single_application(admin_client, make_application):
    application = make_application(name="checkout")
    response = admin_client.get(
        "/api/applications/" + str(application.id), headers=JSON_HEADERS
    )
    assert response.status_code == 200
    assert response.get_json()["name"] == "checkout"
    assert "recent_checks" in response.get_json()


def test_get_missing_application_returns_404(admin_client):
    assert admin_client.get("/api/applications/9999", headers=JSON_HEADERS).status_code == 404


def test_update_application(admin_client, make_application):
    application = make_application(name="old-name")
    response = admin_client.put(
        "/api/applications/" + str(application.id),
        json={"name": "new-name", "environment": "Staging"},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 200
    assert response.get_json()["application"]["name"] == "new-name"
    assert response.get_json()["application"]["environment"] == "Staging"


def test_update_without_changes_is_a_noop(admin_client, make_application):
    application = make_application(name="stable")
    response = admin_client.put(
        "/api/applications/" + str(application.id),
        json={"name": "stable"},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 200
    assert "No changes" in response.get_json()["message"]


def test_duplicate_name_is_rejected(admin_client, make_application):
    make_application(name="duplicate")
    response = create(admin_client, name="duplicate")
    assert response.status_code == 422
    assert "name" in response.get_json()["fields"]


def test_delete_cascades_children(admin_client, make_application, session):
    from models import HealthCheck, Incident

    application = make_application(name="doomed")
    session.add(
        HealthCheck(application_id=application.id, status="UP", response_time=12.0)
    )
    session.add(
        Incident(application_id=application.id, title="child incident", severity="Low")
    )
    session.commit()

    response = admin_client.delete(
        "/api/applications/" + str(application.id), headers=JSON_HEADERS
    )
    assert response.status_code == 200
    assert session.query(HealthCheck).count() == 0
    assert session.query(Incident).count() == 0


def test_pause_and_resume(admin_client, make_application):
    application = make_application()

    paused = admin_client.post(
        "/api/applications/" + str(application.id) + "/pause", headers=JSON_HEADERS
    )
    assert paused.status_code == 200
    assert paused.get_json()["application"]["is_paused"] is True

    resumed = admin_client.post(
        "/api/applications/" + str(application.id) + "/resume", headers=JSON_HEADERS
    )
    assert resumed.get_json()["application"]["is_paused"] is False


def test_search_and_filter(admin_client, make_application):
    make_application(name="alpha-service", environment="Production")
    make_application(name="beta-service", environment="Staging")

    filtered = admin_client.get(
        "/api/applications?search=alpha", headers=JSON_HEADERS
    ).get_json()
    assert len(filtered) == 1
    assert filtered[0]["name"] == "alpha-service"

    by_env = admin_client.get(
        "/api/applications?environment=Staging", headers=JSON_HEADERS
    ).get_json()
    assert len(by_env) == 1
    assert by_env[0]["name"] == "beta-service"


def test_sorting(admin_client, make_application):
    make_application(name="zulu")
    make_application(name="alpha")

    ascending = admin_client.get(
        "/api/applications?sort=name&order=asc", headers=JSON_HEADERS
    ).get_json()
    assert [item["name"] for item in ascending] == ["alpha", "zulu"]


def test_pagination_envelope(admin_client, make_application):
    for index in range(5):
        make_application(name=f"service-{index}")

    response = admin_client.get(
        "/api/applications?page=1&per_page=2", headers=JSON_HEADERS
    )
    payload = response.get_json()

    assert len(payload["data"]) == 2
    assert payload["pagination"]["total"] == 5
    assert payload["pagination"]["total_pages"] == 3
    assert payload["pagination"]["has_next"] is True


def test_metadata_endpoint(admin_client):
    payload = admin_client.get(
        "/api/applications/metadata", headers=JSON_HEADERS
    ).get_json()
    assert "Production" in payload["environments"]
    assert "GET" in payload["http_methods"]


def test_health_history_empty(admin_client, make_application):
    application = make_application()
    response = admin_client.get(
        "/api/applications/" + str(application.id) + "/health-history",
        headers=JSON_HEADERS,
    )
    assert response.status_code == 200
    assert response.get_json()["summary"]["total_checks"] == 0


def test_uptime_report(admin_client, make_application):
    application = make_application()
    response = admin_client.get(
        "/api/applications/" + str(application.id) + "/uptime?days=3",
        headers=JSON_HEADERS,
    )
    payload = response.get_json()
    assert payload["days"] == 3
    assert len(payload["series"]) == 3
