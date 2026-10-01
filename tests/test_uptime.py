"""Uptime accounting tests.

Uptime must reflect a rolling window of recent checks, and a DEGRADED service
is still available, so it must never report 0%.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from conftest import JSON_HEADERS

pytestmark = pytest.mark.integration


def mock_response(status=200, body=b""):
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.status = status
    response.read = MagicMock(return_value=body)
    return response


def check(admin_client, application, response=None):
    with patch(
        "services.health.urlopen",
        return_value=response or mock_response(200),
    ):
        return admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        ).get_json()


def test_first_check_reports_full_uptime(admin_client, make_application):
    application = make_application()
    check(admin_client, application)
    assert application.uptime_percentage == 100.0


def test_degraded_is_not_zero_percent_uptime(admin_client, make_application, app):
    application = make_application()

    # Lower the degradation threshold to zero so any real latency is treated as
    # degraded, without depending on how often perf_counter is called.
    with patch("services.health.SLA_TARGET_MS", 0.0), patch(
        "services.health.urlopen", return_value=mock_response(200)
    ):
        result = admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        ).get_json()

    assert result["health"] == "DEGRADED"
    assert application.uptime_percentage == 100.0


def test_down_check_lowers_uptime(admin_client, make_application):
    from urllib.error import URLError

    application = make_application()
    check(admin_client, application)

    with patch("services.health.urlopen", side_effect=URLError("down")):
        admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        )

    assert application.uptime_percentage == 50.0


def test_uptime_recovers_gradually(admin_client, make_application):
    from urllib.error import URLError

    application = make_application()
    check(admin_client, application)

    with patch("services.health.urlopen", side_effect=URLError("down")):
        admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        )

    assert application.uptime_percentage == 50.0

    check(admin_client, application)
    assert application.uptime_percentage == pytest.approx(66.67, rel=0.01)

    check(admin_client, application)
    assert application.uptime_percentage == pytest.approx(75.0, rel=0.01)


def test_uptime_uses_a_bounded_window(app, admin_client, make_application, session):
    from models import HealthCheck

    app.config["UPTIME_WINDOW_CHECKS"] = 10
    application = make_application()

    # 40 failures, then a success: the window must not include all 40.
    old = datetime.now(UTC) - timedelta(minutes=10)
    for index in range(40):
        session.add(
            HealthCheck(
                application_id=application.id,
                status="DOWN",
                checked_at=old + timedelta(seconds=index),
            )
        )
    session.commit()

    check(admin_client, application)

    assert application.uptime_percentage == 10.0


def test_consecutive_failures_reset_on_success(admin_client, make_application):
    from urllib.error import URLError

    application = make_application()

    for _attempt in range(3):
        with patch("services.health.urlopen", side_effect=URLError("down")):
            admin_client.post(
                "/api/applications/" + str(application.id) + "/check",
                headers=JSON_HEADERS,
            )

    assert application.consecutive_failures == 3

    check(admin_client, application)
    assert application.consecutive_failures == 0


def test_uptime_report_and_field_agree(admin_client, make_application):
    from urllib.error import URLError

    application = make_application()
    check(admin_client, application)

    with patch("services.health.urlopen", side_effect=URLError("down")):
        admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        )

    listed = admin_client.get(
        "/api/applications", headers=JSON_HEADERS
    ).get_json()[0]

    assert listed["uptime_percentage"] == application.uptime_percentage == 50.0
