"""Deployment API tests."""

import pytest
from conftest import JSON_HEADERS

pytestmark = pytest.mark.integration


def create(client, application_id, **overrides):
    payload = {
        "application_id": application_id,
        "version": "v2.4.0",
        "environment": "Production",
        "status": "Successful",
    }
    payload.update(overrides)
    return client.post("/api/deployments", json=payload, headers=JSON_HEADERS)


def test_create_deployment(admin_client, make_application):
    application = make_application()
    response = create(admin_client, application.id)

    assert response.status_code == 201
    deployment = response.get_json()["deployment"]
    assert deployment["version"] == "v2.4.0"
    assert deployment["application_name"] == application.name
    assert deployment["deployed_by"] == "admin"


def test_create_requires_core_fields(admin_client):
    response = admin_client.post(
        "/api/deployments", json={"version": "v1"}, headers=JSON_HEADERS
    )
    assert response.status_code == 422


def test_rejects_invalid_status(admin_client, make_application):
    application = make_application()
    response = create(admin_client, application.id, status="Halfway")
    assert response.status_code == 422
    assert "status" in response.get_json()["fields"]


def test_rejects_negative_duration(admin_client, make_application):
    application = make_application()
    response = create(admin_client, application.id, duration_seconds=-5)
    assert response.status_code == 422
    assert "duration_seconds" in response.get_json()["fields"]


def test_rollback_marks_deployment(admin_client, make_application):
    application = make_application()
    deployment_id = create(admin_client, application.id).get_json()["deployment"]["id"]

    response = admin_client.post(
        "/api/deployments/" + str(deployment_id) + "/rollback", headers=JSON_HEADERS
    )
    assert response.status_code == 200
    assert response.get_json()["deployment"]["status"] == "Rolled Back"
    assert response.get_json()["deployment"]["rolled_back_at"] is not None


def test_double_rollback_is_rejected(admin_client, make_application):
    application = make_application()
    deployment_id = create(admin_client, application.id).get_json()["deployment"]["id"]

    admin_client.post(
        "/api/deployments/" + str(deployment_id) + "/rollback", headers=JSON_HEADERS
    )
    response = admin_client.post(
        "/api/deployments/" + str(deployment_id) + "/rollback", headers=JSON_HEADERS
    )
    assert response.status_code == 409


def test_update_deployment(admin_client, make_application):
    application = make_application()
    deployment_id = create(admin_client, application.id).get_json()["deployment"]["id"]

    response = admin_client.put(
        "/api/deployments/" + str(deployment_id),
        json={"status": "Failed", "changelog": "bad release"},
        headers=JSON_HEADERS,
    )
    assert response.get_json()["deployment"]["status"] == "Failed"
    assert response.get_json()["deployment"]["changelog"] == "bad release"


def test_delete_deployment(admin_client, make_application):
    application = make_application()
    deployment_id = create(admin_client, application.id).get_json()["deployment"]["id"]

    assert admin_client.delete(
        "/api/deployments/" + str(deployment_id), headers=JSON_HEADERS
    ).status_code == 200


def test_summary_endpoint(admin_client, make_application):
    application = make_application()
    create(admin_client, application.id, status="Successful")
    create(admin_client, application.id, version="v2.5.0", status="Failed")

    response = admin_client.get(
        "/api/deployments?per_page=10", headers=JSON_HEADERS
    ).get_json()

    assert response["pagination"]["total"] == 2
    assert response["summary"]["by_status"]["Successful"] == 1
    assert response["summary"]["by_status"]["Failed"] == 1


def test_filtering(admin_client, make_application):
    application = make_application()
    create(admin_client, application.id, environment="Production")
    create(admin_client, application.id, version="v9", environment="Staging")

    staged = admin_client.get(
        "/api/deployments?environment=Staging", headers=JSON_HEADERS
    ).get_json()
    assert len(staged) == 1
    assert staged[0]["version"] == "v9"


def test_linked_incidents(admin_client, make_application):
    application = make_application()
    deployment_id = create(admin_client, application.id).get_json()["deployment"]["id"]

    admin_client.post(
        "/api/incidents",
        json={"application_id": application.id, "title": "post deploy outage"},
        headers=JSON_HEADERS,
    )

    response = admin_client.get(
        "/api/deployments/" + str(deployment_id) + "/incidents", headers=JSON_HEADERS
    )
    assert response.status_code == 200
    assert len(response.get_json()["linked_incidents"]) == 1
