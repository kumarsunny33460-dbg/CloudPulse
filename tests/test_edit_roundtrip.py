"""The edit form must be able to round-trip every field it renders.

A field the UI displays but the API does not return is worse than a missing
feature: the form looks correct, the operator saves, and the value is silently
discarded.
"""

from __future__ import annotations

from conftest import JSON_HEADERS

# Everything applications.js reads out of `monitoring` when opening the edit
# form. If the serializer omits one, that field vanishes on the next save.
MONITORING_FIELDS = (
    "http_method",
    "timeout_seconds",
    "check_interval_seconds",
    "expected_status_codes",
    "expected_body_keyword",
    "request_headers",
    "is_paused",
)


def test_monitoring_summary_exposes_every_field_the_edit_form_reads():
    from models import Application

    summary = Application.monitoring_summary(
        Application(
            http_method="GET",
            timeout_seconds=5,
            check_interval_seconds=60,
            expected_status_codes="404",
            expected_body_keyword="ok",
            request_headers='{"Authorization": "Bearer abc"}',
            is_paused=False,
        )
    )

    for field in MONITORING_FIELDS:
        assert field in summary, f"monitoring_summary is missing '{field}'"

    assert summary["request_headers"] == '{"Authorization": "Bearer abc"}'


def test_custom_headers_survive_an_edit_round_trip(
    admin_client, make_application, session
):
    """Open the form, change one field, save, and headers must remain."""
    application = make_application(
        url="https://api.example.com/health",
        request_headers='{"Authorization": "Bearer secret-token"}',
        expected_status_codes="202",
    )

    status, detail = _get(admin_client, application.id)
    assert status == 200
    assert detail["monitoring"]["request_headers"] == '{"Authorization": "Bearer secret-token"}'

    # Simulate the browser: send back the monitoring block the form showed.
    monitoring = detail["monitoring"]
    response = admin_client.put(
        f"/api/applications/{application.id}",
        headers=JSON_HEADERS,
        json={
            "url": "https://api.example.com/health",
            "http_method": monitoring["http_method"],
            "timeout_seconds": monitoring["timeout_seconds"],
            "check_interval_seconds": monitoring["check_interval_seconds"],
            "expected_status_codes": monitoring["expected_status_codes"],
            "expected_body_keyword": monitoring["expected_body_keyword"],
            "request_headers": monitoring["request_headers"],
        },
    )
    assert response.status_code == 200

    session.expire_all()
    from models import Application

    stored = session.get(Application, application.id)
    assert stored.request_headers == '{"Authorization": "Bearer secret-token"}'
    assert stored.expected_status_codes == "202"


def test_status_code_specification_is_not_reset_by_a_partial_update(
    admin_client, make_application, session
):
    """Editing only the URL must not fall back to the default 200-399."""
    application = make_application(
        url="https://legacy.example.com/report",
        expected_status_codes="404",
    )

    response = admin_client.put(
        f"/api/applications/{application.id}",
        headers=JSON_HEADERS,
        json={"url": "https://legacy.example.com/report-v2"},
    )
    assert response.status_code == 200

    session.expire_all()
    from models import Application

    stored = session.get(Application, application.id)
    assert stored.expected_status_codes == "404"
    assert stored.url == "https://legacy.example.com/report-v2"


def test_health_check_still_uses_the_custom_specification(
    admin_client, make_application
):
    """A 404 endpoint configured to expect 404 must report as healthy."""
    from unittest.mock import MagicMock, patch

    application = make_application(
        url="https://legacy.example.com/report",
        expected_status_codes="404",
    )

    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.status = 404
    response.read = MagicMock(return_value=b"gone")

    with patch("services.health.urlopen", return_value=response):
        result = admin_client.post(
            f"/api/applications/{application.id}/check", headers=JSON_HEADERS
        ).get_json()

    assert result["health"] == "UP"


def _get(client, application_id):
    response = client.get(f"/api/applications/{application_id}", headers=JSON_HEADERS)
    return response.status_code, response.get_json()
