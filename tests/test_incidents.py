"""Incident management API and service tests."""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import JSON_HEADERS

pytestmark = pytest.mark.integration


def create(client, application_id, **overrides):
    payload = {
        "application_id": application_id,
        "title": "Checkout latency spike",
        "description": "p99 above 4 seconds",
        "severity": "High",
        "status": "Open",
    }
    payload.update(overrides)
    return client.post("/api/incidents", json=payload, headers=JSON_HEADERS)


def test_create_incident(admin_client, make_application):
    application = make_application()
    response = create(admin_client, application.id)

    assert response.status_code == 201
    incident = response.get_json()["incident"]
    assert incident["title"] == "Checkout latency spike"
    assert incident["application_name"] == application.name
    assert incident["source"] == "manual"


def test_create_requires_application_and_title(admin_client):
    response = admin_client.post(
        "/api/incidents", json={"title": "orphan"}, headers=JSON_HEADERS
    )
    assert response.status_code == 422


def test_rejects_unknown_application(admin_client):
    response = create(admin_client, 4242)
    assert response.status_code == 422
    assert "application_id" in response.get_json()["fields"]


def test_rejects_invalid_severity(admin_client, make_application):
    application = make_application()
    response = create(admin_client, application.id, severity="Catastrophic")
    assert response.status_code == 422
    assert "severity" in response.get_json()["fields"]


def test_resolve_incident_sets_timestamps(admin_client, make_application):
    application = make_application()
    incident_id = create(admin_client, application.id).get_json()["incident"]["id"]

    response = admin_client.put(
        "/api/incidents/" + str(incident_id) + "/resolve", json={}, headers=JSON_HEADERS
    )
    assert response.status_code == 200

    payload = response.get_json()["incident"]
    assert payload["status"] == "Resolved"
    assert payload["resolved_at"] is not None
    assert payload["duration_minutes"] is not None


def test_reopening_clears_resolution(admin_client, make_application):
    application = make_application()
    incident_id = create(admin_client, application.id).get_json()["incident"]["id"]

    admin_client.put(
        "/api/incidents/" + str(incident_id) + "/resolve", json={}, headers=JSON_HEADERS
    )
    response = admin_client.put(
        "/api/incidents/" + str(incident_id),
        json={"status": "Open"},
        headers=JSON_HEADERS,
    )
    assert response.get_json()["incident"]["resolved_at"] is None


def test_acknowledge_moves_to_investigating(admin_client, make_application):
    application = make_application()
    incident_id = create(admin_client, application.id).get_json()["incident"]["id"]

    response = admin_client.post(
        "/api/incidents/" + str(incident_id) + "/acknowledge", headers=JSON_HEADERS
    )
    assert response.get_json()["incident"]["status"] == "Investigating"
    assert response.get_json()["incident"]["acknowledged_at"] is not None


def test_escalate_raises_severity(admin_client, make_application):
    application = make_application()
    incident_id = create(
        admin_client, application.id, severity="Low"
    ).get_json()["incident"]["id"]

    response = admin_client.post(
        "/api/incidents/" + str(incident_id) + "/escalate", headers=JSON_HEADERS
    )
    assert response.get_json()["incident"]["severity"] == "Medium"


def test_escalate_stops_at_critical(admin_client, make_application):
    application = make_application()
    incident_id = create(
        admin_client, application.id, severity="Critical"
    ).get_json()["incident"]["id"]

    response = admin_client.post(
        "/api/incidents/" + str(incident_id) + "/escalate", headers=JSON_HEADERS
    )
    assert response.status_code == 409


def test_sla_fields_are_exposed(admin_client, make_application):
    application = make_application()
    incident_id = create(
        admin_client, application.id, severity="Critical"
    ).get_json()["incident"]["id"]

    payload = admin_client.get(
        "/api/incidents/" + str(incident_id), headers=JSON_HEADERS
    ).get_json()

    assert payload["sla_minutes"] == 30
    assert payload["sla_breached"] is False
    assert payload["sla_remaining_minutes"] > 0


def test_sla_breach_detection(admin_client, make_application, session):
    from models import Incident

    application = make_application()
    stale = datetime.now(UTC) - timedelta(minutes=120)
    incident = Incident(
        application=application,
        title="Old critical outage",
        severity="Critical",
        status="Open",
        detected_at=stale,
    )
    session.add(incident)
    session.commit()

    response = admin_client.get("/api/incidents/stats", headers=JSON_HEADERS)
    assert response.get_json()["reliability"]["sla_breaches"] >= 1


def test_delete_incident(admin_client, make_application):
    application = make_application()
    incident_id = create(admin_client, application.id).get_json()["incident"]["id"]

    assert admin_client.delete(
        "/api/incidents/" + str(incident_id), headers=JSON_HEADERS
    ).status_code == 200
    assert admin_client.get(
        "/api/incidents/" + str(incident_id), headers=JSON_HEADERS
    ).status_code == 404


def test_bulk_resolve(admin_client, make_application):
    application = make_application()
    first = create(admin_client, application.id, title="one").get_json()["incident"]["id"]
    second = create(admin_client, application.id, title="two").get_json()["incident"]["id"]

    response = admin_client.post(
        "/api/incidents/bulk",
        json={"action": "resolve", "ids": [first, second]},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 200
    assert response.get_json()["updated"] == 2


def test_bulk_rejects_unknown_action(admin_client, make_application):
    application = make_application()
    incident_id = create(admin_client, application.id).get_json()["incident"]["id"]

    response = admin_client.post(
        "/api/incidents/bulk",
        json={"action": "explode", "ids": [incident_id]},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 400


def test_filters(admin_client, make_application):
    application = make_application()
    create(admin_client, application.id, title="open one", severity="High")
    create(
        admin_client,
        application.id,
        title="resolved one",
        severity="Low",
        status="Resolved",
    )

    open_only = admin_client.get(
        "/api/incidents?open=true", headers=JSON_HEADERS
    ).get_json()
    assert len(open_only) == 1
    assert open_only[0]["title"] == "open one"

    by_severity = admin_client.get(
        "/api/incidents?severity=Low", headers=JSON_HEADERS
    ).get_json()
    assert len(by_severity) == 1


def test_timeline_records_activity(admin_client, make_application):
    application = make_application()
    incident_id = create(admin_client, application.id).get_json()["incident"]["id"]
    admin_client.put(
        "/api/incidents/" + str(incident_id) + "/resolve", json={}, headers=JSON_HEADERS
    )

    detail = admin_client.get(
        "/api/incidents/" + str(incident_id), headers=JSON_HEADERS
    ).get_json()

    actions = [entry["action"] for entry in detail["timeline"]]
    assert "incident.created" in actions
    assert "incident.resolved" in actions


def test_reliability_statistics(admin_client, make_application, session):
    from models import Incident

    application = make_application()
    now = datetime.now(UTC)

    session.add(
        Incident(
            application=application,
            title="old",
            severity="High",
            status="Resolved",
            detected_at=now - timedelta(hours=4),
            resolved_at=now - timedelta(hours=3),
        )
    )
    session.commit()

    payload = admin_client.get(
        "/api/incidents/stats?days=30", headers=JSON_HEADERS
    ).get_json()

    assert payload["reliability"]["resolved_incidents"] == 1
    assert payload["reliability"]["mttr_minutes"] == pytest.approx(60.0, rel=0.05)
