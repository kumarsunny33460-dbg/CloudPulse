"""Health checking, monitoring cycle and notification service tests.

Network access is never used: ``urlopen`` is patched so the suite stays fast
and deterministic.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from conftest import JSON_HEADERS

pytestmark = pytest.mark.integration


def mock_response(status=200, body=b"", read_error=None):
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.status = status
    response.read = MagicMock(return_value=body)
    if read_error is not None:
        response.read = MagicMock(side_effect=read_error)
    return response


# ---------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected_error",
    [
        ("https://example.com", None),
        ("http://example.com/health", None),
        ("", "required"),
        ("ftp://example.com", "not allowed"),
        ("file:///etc/passwd", "not allowed"),
        ("example.com", "not allowed"),
        ("https://", "host name"),
    ],
)
def test_validate_url(app, url, expected_error):
    from services.health import validate_url

    result = validate_url(url)
    if expected_error is None:
        assert result is None
    else:
        assert result is not None
        assert expected_error.lower() in result.lower()


# ---------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------

def test_probe_success(app, make_application):
    from services.health import probe

    application = make_application()

    with patch("services.health.urlopen", return_value=mock_response(200)):
        result = probe(application)

    assert result["health"] == "UP"
    assert result["http_status"] == 200
    assert result["response_time_ms"] >= 0


def test_probe_degraded_on_slow_response(app, make_application):
    from services.health import probe

    application = make_application()

    with patch("services.health.urlopen", return_value=mock_response(200)), patch(
        "services.health.time.perf_counter", side_effect=[0.0, 2.5]
    ):
        result = probe(application)

    assert result["health"] == "DEGRADED"
    assert "Slow response" in result["message"]


def test_probe_marks_unexpected_status_down(app, make_application):
    from services.health import probe

    application = make_application()

    with patch("services.health.urlopen", side_effect=__import__(
        "urllib.error", fromlist=["HTTPError"]
    ).HTTPError("http://x", 500, "boom", {}, None)):
        result = probe(application)

    assert result["health"] == "DOWN"
    assert result["http_status"] == 500


def test_probe_accepts_configured_status_codes(app, make_application):
    from services.health import probe

    application = make_application(expected_status_codes="404")

    http_error = __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
        "http://x", 404, "missing", {}, None
    )
    with patch("services.health.urlopen", side_effect=http_error):
        result = probe(application)

    assert result["health"] == "UP"
    assert result["http_status"] == 404


def test_probe_enforces_body_keyword(app, make_application):
    from services.health import probe

    application = make_application(expected_body_keyword="healthy")

    with patch("services.health.urlopen", return_value=mock_response(200, b'{"status":"healthy"}')):
        assert probe(application)["health"] == "UP"

    with patch("services.health.urlopen", return_value=mock_response(200, b'{"status":"bad"}')):
        result = probe(application)

    assert result["health"] == "DOWN"
    assert "did not contain" in result["message"]


def test_probe_handles_invalid_url(app, make_application):
    from services.health import probe

    application = make_application(url="ftp://example.com")
    result = probe(application)

    assert result["health"] == "DOWN"
    assert result["http_status"] is None


def test_probe_handles_unreachable_host(app, make_application):
    from urllib.error import URLError

    from services.health import probe

    application = make_application()

    with patch("services.health.urlopen", side_effect=URLError("no route to host")):
        result = probe(application)

    assert result["health"] == "DOWN"
    assert "unreachable" in result["message"]


# ---------------------------------------------------------------------
# Persistence and incident reconciliation
# ---------------------------------------------------------------------

def test_check_persists_health_check_row(app, admin_client, make_application, session):
    from models import HealthCheck

    application = make_application()

    with patch("services.health.urlopen", return_value=mock_response(200)):
        admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        )

    checks = session.query(HealthCheck).filter_by(application_id=application.id).all()
    assert len(checks) == 1
    assert checks[0].status == "UP"

    application = session.get(type(application), application.id)
    assert application.last_health_status == "UP"
    assert application.consecutive_failures == 0


def test_down_check_opens_incident(app, admin_client, make_application, session):
    from urllib.error import URLError

    from models import Incident

    application = make_application()

    with patch("services.health.urlopen", side_effect=URLError("down")):
        admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        )

    incidents = session.query(Incident).filter_by(application_id=application.id).all()
    assert len(incidents) == 1
    assert incidents[0].severity == "High"
    assert incidents[0].source == "monitoring"


def test_repeated_failures_do_not_duplicate_incidents(
    app, admin_client, make_application, session
):
    from urllib.error import URLError

    from models import Incident

    application = make_application()

    for _attempt in range(3):
        with patch("services.health.urlopen", side_effect=URLError("down")):
            admin_client.post(
                "/api/applications/" + str(application.id) + "/check",
                headers=JSON_HEADERS,
            )

    incidents = session.query(Incident).filter_by(application_id=application.id).all()
    assert len(incidents) == 1


def test_recovery_auto_resolves_incident(app, admin_client, make_application, session):
    from urllib.error import URLError

    from models import Incident

    application = make_application()

    with patch("services.health.urlopen", side_effect=URLError("down")):
        admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        )

    with patch("services.health.urlopen", return_value=mock_response(200)):
        admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        )

    incident = session.query(Incident).filter_by(application_id=application.id).one()
    assert incident.status == "Resolved"
    assert incident.resolved_at is not None


def test_manual_incident_is_not_auto_resolved(app, admin_client, make_application, session):
    from models import Incident

    application = make_application()
    session.add(
        Incident(
            application=application,
            title="Manually raised",
            severity="Low",
            status="Open",
            source="manual",
        )
    )
    session.commit()

    with patch("services.health.urlopen", return_value=mock_response(200)):
        admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        )

    incident = session.query(Incident).filter_by(application_id=application.id).one()
    assert incident.status == "Open"


def test_legacy_health_endpoint_still_works(admin_client, make_application):
    application = make_application()

    with patch("services.health.urlopen", return_value=mock_response(200)):
        response = admin_client.get(
            "/api/applications/" + str(application.id) + "/health", headers=JSON_HEADERS
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["health"] == "UP"
    assert "incident_created" in payload


# ---------------------------------------------------------------------
# Monitoring cycle
# ---------------------------------------------------------------------

def test_monitoring_cycle_summary(app, admin_client, make_application):
    make_application(name="up-app")
    make_application(name="down-app", url="https://down.example.com")

    def fake_urlopen(request, timeout=None, context=None):
        if "down.example.com" in request.full_url:
            raise __import__("urllib.error", fromlist=["URLError"]).URLError("down")
        return mock_response(200)

    with patch("services.health.urlopen", side_effect=fake_urlopen):
        payload = admin_client.get("/api/monitor", headers=JSON_HEADERS).get_json()

    assert payload["summary"]["total"] == 2
    assert payload["summary"]["healthy"] == 1
    assert payload["summary"]["unhealthy"] == 1
    assert payload["summary"]["open_incidents"] == 1
    assert "degraded" in payload["summary"]


def test_monitor_run_requires_write_role(viewer_client, make_application):
    make_application()
    response = viewer_client.post("/api/monitor/run", headers=JSON_HEADERS)
    assert response.status_code == 403


def test_paused_applications_are_skipped_by_scheduler(app, make_application, session):
    from services.health import is_due, run_monitoring_cycle

    active = make_application(name="active")
    paused = make_application(name="paused")
    paused.is_paused = True
    session.commit()

    now = datetime.now(UTC)
    assert is_due(active, now) is True
    assert is_due(paused, now) is False

    with patch("services.health.urlopen", return_value=mock_response(200)):
        result = run_monitoring_cycle(force=False)

    assert [item["name"] for item in result["applications"]] == ["active"]


def test_decommissioned_applications_are_skipped(app, make_application, session):
    from services.health import is_due

    application = make_application(status="Decommissioned")
    assert is_due(application, datetime.now(UTC)) is False


def test_is_due_respects_interval(app, make_application, session):
    from services.health import is_due

    application = make_application(check_interval_seconds=60)
    now = datetime.now(UTC)

    application.last_checked_at = now - timedelta(seconds=120)
    session.commit()
    assert is_due(application, now) is True

    application.last_checked_at = now - timedelta(seconds=10)
    session.commit()
    assert is_due(application, now) is False


# ---------------------------------------------------------------------
# History, retention and exports
# ---------------------------------------------------------------------

def test_health_history_summary(app, admin_client, make_application):
    application = make_application()

    for _attempt in range(4):
        with patch("services.health.urlopen", return_value=mock_response(200)):
            admin_client.post(
                "/api/applications/" + str(application.id) + "/check",
                headers=JSON_HEADERS,
            )

    payload = admin_client.get(
        "/api/applications/" + str(application.id) + "/health-history?limit=10",
        headers=JSON_HEADERS,
    ).get_json()

    assert payload["summary"]["total_checks"] == 4
    assert payload["summary"]["up_checks"] == 4
    assert payload["summary"]["uptime_percentage"] == 100.0
    assert payload["summary"]["avg_response_time"] is not None


def test_retention_prunes_old_rows(app, admin_client, make_application, session):
    from models import HealthCheck
    from services.health import prune_health_checks

    application = make_application()
    stale = datetime.now(UTC) - timedelta(days=400)
    session.add(HealthCheck(application_id=application.id, status="UP", checked_at=stale))
    session.commit()

    removed = prune_health_checks(30)
    assert removed == 1
    assert session.query(HealthCheck).count() == 0


def test_retention_endpoint(app, admin_client, make_application, session):
    from models import HealthCheck

    application = make_application()
    stale = datetime.now(UTC) - timedelta(days=90)
    session.add(HealthCheck(application_id=application.id, status="UP", checked_at=stale))
    session.commit()

    response = admin_client.post(
        "/api/monitor/retention?days=30", headers=JSON_HEADERS
    )
    assert response.status_code == 200
    assert response.get_json()["removed"] == 1


def test_health_check_csv_export(admin_client, make_application):
    application = make_application()

    with patch("services.health.urlopen", return_value=mock_response(200)):
        admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        )

    response = admin_client.get(
        "/api/export/health-checks", headers=JSON_HEADERS
    )
    assert response.status_code == 200
    assert "text/csv" in response.headers["Content-Type"]
    assert "application_id" in response.get_data(as_text=True)
