"""Observability tests: overview aggregation, Prometheus export, activity."""

from unittest.mock import patch

import pytest
from conftest import JSON_HEADERS

pytestmark = pytest.mark.integration


def test_overview_shape(admin_client, make_application):
    make_application(name="alpha")
    make_application(name="beta", environment="Staging")

    payload = admin_client.get("/api/overview", headers=JSON_HEADERS).get_json()

    assert payload["applications"]["total"] == 2
    assert payload["applications"]["active"] == 2
    assert payload["incidents"]["open"] == 0
    assert payload["reliability"]["sla_compliance_percentage"] == 100.0
    assert payload["deployments"]["total"] == 0


def test_overview_counts_health_states(app, admin_client, make_application, session):

    healthy = make_application(name="healthy")
    healthy.last_health_status = "UP"
    down = make_application(name="down")
    down.last_health_status = "DOWN"
    session.commit()

    payload = admin_client.get("/api/overview", headers=JSON_HEADERS).get_json()

    assert payload["applications"]["healthy"] == 1
    assert payload["applications"]["unhealthy"] == 1


def test_timeseries_returns_aligned_series(admin_client):
    payload = admin_client.get(
        "/api/overview/timeseries?hours=6", headers=JSON_HEADERS
    ).get_json()

    assert payload["hours"] == 6
    assert len(payload["labels"]) == len(payload["checks"])
    assert len(payload["labels"]) == len(payload["failures"])
    assert len(payload["labels"]) == len(payload["avg_response_time_ms"])


def test_leaderboard_orders_by_uptime(app, admin_client, make_application, session):

    worst = make_application(name="worst")
    worst.uptime_percentage = 10.0
    best = make_application(name="best")
    best.uptime_percentage = 99.99
    session.commit()

    payload = admin_client.get(
        "/api/overview/leaderboard", headers=JSON_HEADERS
    ).get_json()

    assert payload[0]["name"] == "worst"
    assert payload[-1]["name"] == "best"


def test_monitoring_summary(admin_client, make_application):
    make_application(name="summary-target")
    payload = admin_client.get("/api/monitor/summary", headers=JSON_HEADERS).get_json()

    assert payload["total_applications"] == 1
    assert payload["active_applications"] == 1
    assert payload["never_checked"] == 1
    assert payload["paused_applications"] == 0


def test_monitoring_history(admin_client, make_application):
    make_application()
    response = admin_client.get(
        "/api/monitor/history?hours=24", headers=JSON_HEADERS
    )
    assert response.status_code == 200
    assert response.get_json() == []


def test_fleet_uptime_report(admin_client, make_application):
    make_application()
    payload = admin_client.get(
        "/api/monitor/uptime?days=2", headers=JSON_HEADERS
    ).get_json()

    assert payload["days"] == 2
    assert len(payload["applications"]) == 1


# ---------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------

def test_metrics_endpoint_exposes_expected_series(admin_client, make_application):
    application = make_application(name="prom-target")
    application.last_health_status = "UP"
    application.last_response_time = 42.0

    body = admin_client.get("/metrics").get_data(as_text=True)

    assert "cloudpulse_build_info" in body
    assert "cloudpulse_application_up" in body
    assert "cloudpulse_incidents_open" in body
    assert "cloudpulse_http_requests_total" in body
    assert "cloudpulse_application_info" in body
    assert "prom-target" in body
    assert 'application_up{application="prom-target",environment="Production",id="' in body


def test_metrics_content_type(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["Content-Type"]


def test_metrics_records_http_requests(client):
    client.get("/metrics")
    body = client.get("/metrics").get_data(as_text=True)
    assert 'cloudpulse_http_requests_total{method="GET",route="/metrics",status="200"}' in body


def test_metrics_includes_histogram(client):
    client.get("/health")
    body = client.get("/metrics").get_data(as_text=True)
    assert "cloudpulse_http_request_duration_seconds_bucket" in body
    assert "cloudpulse_http_request_duration_seconds_count" in body
    assert "cloudpulse_http_request_duration_seconds_sum" in body


# ---------------------------------------------------------------------
# Activity and exports
# ---------------------------------------------------------------------

def test_activity_records_application_lifecycle(admin_client, make_application):
    application = make_application(name="audited")
    admin_client.put(
        "/api/applications/" + str(application.id),
        json={"description": "updated"},
        headers=JSON_HEADERS,
    )
    admin_client.delete("/api/applications/" + str(application.id), headers=JSON_HEADERS)

    entries = admin_client.get("/api/activity", headers=JSON_HEADERS).get_json()
    actions = [entry["action"] for entry in entries]

    assert "application.updated" in actions
    assert "application.deleted" in actions
    assert all(entry["actor_name"] == "admin" for entry in entries)


def test_activity_filtering(admin_client, make_application):
    application = make_application()
    admin_client.post(
        "/api/incidents",
        json={"application_id": application.id, "title": "audited incident"},
        headers=JSON_HEADERS,
    )

    payload = admin_client.get(
        "/api/activity?entity_type=incident", headers=JSON_HEADERS
    ).get_json()
    assert len(payload) == 1
    assert payload[0]["entity_type"] == "incident"


def test_activity_pagination(admin_client):
    for index in range(4):
        admin_client.post(
            "/api/applications",
            json={"name": f"app-{index}", "url": "https://example.com", "environment": "Production"},
            headers=JSON_HEADERS,
        )

    payload = admin_client.get(
        "/api/activity?page=1&per_page=2", headers=JSON_HEADERS
    ).get_json()

    assert len(payload["data"]) == 2
    assert payload["pagination"]["total"] == 4


def test_activity_export(admin_client, make_application):
    make_application()
    response = admin_client.get("/api/export/activity", headers=JSON_HEADERS)
    assert response.status_code == 200
    assert "action" in response.get_data(as_text=True)


def test_applications_export(admin_client, make_application):
    make_application(name="exported")
    response = admin_client.get("/api/export/applications", headers=JSON_HEADERS)

    body = response.get_data(as_text=True)
    assert "exported" in body
    assert "uptime_percentage" in body
    assert "attachment" in response.headers["Content-Disposition"]


def test_incidents_export(admin_client, make_application):
    application = make_application()
    admin_client.post(
        "/api/incidents",
        json={"application_id": application.id, "title": "exported incident"},
        headers=JSON_HEADERS,
    )

    body = admin_client.get("/api/export/incidents", headers=JSON_HEADERS).get_data(as_text=True)
    assert "exported incident" in body


def test_deployments_export(admin_client, make_application):
    application = make_application()
    admin_client.post(
        "/api/deployments",
        json={
            "application_id": application.id,
            "version": "v7.7.7",
            "environment": "Production",
        },
        headers=JSON_HEADERS,
    )

    body = admin_client.get("/api/export/deployments", headers=JSON_HEADERS).get_data(as_text=True)
    assert "v7.7.7" in body


def test_system_info_endpoint(admin_client):
    payload = admin_client.get("/api/system", headers=JSON_HEADERS).get_json()
    assert payload["application"] == "CloudPulse"
    assert payload["database"] == "sqlite"


# ---------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------

def test_webhook_failure_is_recorded_not_raised(app, admin_client, make_application, session):
    from urllib.error import URLError

    from models import Notification

    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/services/xxx"
    application = make_application()

    with patch("services.notifications.urlopen", side_effect=URLError("no route")):
        admin_client.post(
            "/api/incidents",
            json={"application_id": application.id, "title": "notify failure"},
            headers=JSON_HEADERS,
        )

    notification = session.query(Notification).one()
    assert notification.status == "failed"
    assert notification.channel == "webhook"
    assert notification.attempts == 1


def test_webhook_success_is_recorded(app, admin_client, make_application, session):
    from models import Notification

    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/services/xxx"
    application = make_application()

    with patch("services.notifications.urlopen") as mocked:
        mocked.return_value.__enter__.return_value.status = 200
        admin_client.post(
            "/api/incidents",
            json={"application_id": application.id, "title": "notify success"},
            headers=JSON_HEADERS,
        )

    notification = session.query(Notification).one()
    assert notification.status == "sent"
    assert notification.sent_at is not None


def test_notifications_can_be_disabled(app, admin_client, make_application, session):
    from models import Notification

    app.config["NOTIFY_ON_INCIDENT_OPEN"] = False
    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/services/xxx"
    application = make_application()

    with patch("services.notifications.urlopen") as mocked:
        mocked.return_value.__enter__.return_value.status = 200
        admin_client.post(
            "/api/incidents",
            json={"application_id": application.id, "title": "quiet"},
            headers=JSON_HEADERS,
        )

    assert session.query(Notification).count() == 0
