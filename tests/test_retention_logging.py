"""Tests for the retention jobs and the structured logging formatter."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from conftest import JSON_HEADERS

# ---------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------

def _age_rows(session, model, column, days, count=3, **kwargs):
    created = []
    for _ in range(count):
        row = model(
            **kwargs,
            **{column: datetime.now(UTC) - timedelta(days=days)},
        )
        session.add(row)
        created.append(row)
    session.commit()
    return created


def test_audit_logs_older_than_the_window_are_removed(app, admin_client, session):
    from models import AuditLog
    from services import retention

    _age_rows(session, AuditLog, "created_at", 200, count=3, action="old.event")

    removed = retention.prune_audit_logs(180)
    assert removed == 3
    assert session.query(AuditLog).count() == 0


def test_recent_audit_logs_survive(app, admin_client, session):
    from models import AuditLog
    from services import retention

    _age_rows(session, AuditLog, "created_at", 5, count=2, action="new.event")

    assert retention.prune_audit_logs(180) == 0
    assert session.query(AuditLog).count() == 2


def test_old_notifications_are_removed(app, session):
    from models import Notification
    from services import retention

    _age_rows(
        session,
        Notification,
        "created_at",
        60,
        count=2,
        channel="log",
        event="incident.opened",
        status="skipped",
    )

    assert retention.prune_notifications(30) == 2
    assert session.query(Notification).count() == 0


def test_zero_retention_disables_pruning(app, admin_client, session):
    from models import AuditLog
    from services import retention

    _age_rows(session, AuditLog, "created_at", 900, count=1, action="ancient")

    assert retention.prune_audit_logs(0) == 0
    assert session.query(AuditLog).count() == 1


def test_retention_uses_configured_defaults(app, admin_client, session):
    from models import AuditLog
    from services import retention

    app.config["AUDIT_LOG_RETENTION_DAYS"] = 10
    _age_rows(session, AuditLog, "created_at", 30, count=4, action="event")

    assert retention.prune_audit_logs() == 4


def test_prune_all_reports_every_table(app, admin_client, session, make_application):
    from models import HealthCheck
    from services import retention

    application = make_application()
    session.add(
        HealthCheck(
            application_id=application.id,
            status="UP",
            checked_at=datetime.now(UTC) - timedelta(days=400),
        )
    )
    session.commit()

    summary = retention.prune_all()

    for key in (
        "health_checks",
        "audit_logs",
        "notifications",
        "auth_tokens",
        "rate_limit_keys",
    ):
        assert key in summary

    assert summary["health_checks"] >= 1


def test_prune_all_is_idempotent(app, admin_client, session):
    from services import retention

    first = retention.prune_all()
    second = retention.prune_all()
    assert second["health_checks"] == 0
    assert first is not None


def test_prune_all_rolls_back_everything_when_one_step_fails(
    app, admin_client, session, make_application, monkeypatch
):
    """A partial failure must not leave the database half pruned.

    The individual helpers used to commit internally, so a later failure left
    earlier deletes already durable with no record of which half ran.
    """
    from models import HealthCheck, Notification
    from services import retention

    application = make_application()
    session.add(
        HealthCheck(
            application_id=application.id,
            status="UP",
            checked_at=datetime.now(UTC) - timedelta(days=400),
        )
    )
    session.add(
        Notification(
            application_id=application.id,
            channel="log",
            event="incident.opened",
            status="skipped",
            created_at=datetime.now(UTC) - timedelta(days=400),
        )
    )
    session.commit()

    def explode(*args, **kwargs):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(
        "services.accounts.prune_expired_tokens", explode, raising=True
    )

    with pytest.raises(retention.RetentionError):
        retention.prune_all()

    session.expire_all()
    assert session.query(HealthCheck).count() == 1
    assert session.query(Notification).count() == 1


def test_a_failed_prune_is_not_reported_as_success(
    app, admin_client, session, monkeypatch
):
    """A total failure used to return 200 "Pruned 0 records"."""
    from services import retention

    def explode():
        raise retention.RetentionError("database is read-only")

    monkeypatch.setattr("services.retention.prune_all", explode, raising=True)

    response = admin_client.post("/api/monitor/retention", headers=JSON_HEADERS)

    assert response.status_code == 500
    assert b"no rows were removed" in response.data
    assert b"Pruned 0 records" not in response.data


# ---------------------------------------------------------------------
# Retention API
# ---------------------------------------------------------------------

def test_retention_status_endpoint_requires_login(app, client):
    response = client.get("/api/monitor/retention", headers=JSON_HEADERS)
    assert response.status_code == 401


def test_retention_status_reports_counts_and_policies(
    app, admin_client, make_application, session
):
    from models import HealthCheck

    application = make_application()
    session.add(HealthCheck(application_id=application.id, status="UP"))
    session.commit()

    response = admin_client.get("/api/monitor/retention", headers=JSON_HEADERS)
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["counts"]["health_checks"] == 1
    assert "health_checks_days" in payload["policies"]
    assert "audit_logs_days" in payload["policies"]
    assert payload["policies"]["job_interval_hours"] == 6


def test_retention_status_is_read_only(app, admin_client, session):
    from models import AuditLog

    _age_rows(session, AuditLog, "created_at", 900, count=2, action="old")

    admin_client.get("/api/monitor/retention", headers=JSON_HEADERS)
    assert session.query(AuditLog).count() == 2


def test_retention_prune_endpoint_breaks_down_by_table(
    app, admin_client, session, make_application
):
    from models import AuditLog, HealthCheck

    application = make_application()
    session.add(
        HealthCheck(
            application_id=application.id,
            status="UP",
            checked_at=datetime.now(UTC) - timedelta(days=90),
        )
    )
    _age_rows(session, AuditLog, "created_at", 400, count=1, action="old")
    session.commit()

    response = admin_client.post(
        "/api/monitor/retention?days=30", headers=JSON_HEADERS
    )
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["removed"] == 2
    assert payload["removed_by_table"]["health_checks"] == 1
    assert payload["removed_by_table"]["audit_logs"] == 1


def test_retention_without_days_uses_configured_policies(
    app, admin_client, session
):
    from models import AuditLog

    app.config["AUDIT_LOG_RETENTION_DAYS"] = 30
    _age_rows(session, AuditLog, "created_at", 60, count=2, action="old")
    session.commit()

    response = admin_client.post("/api/monitor/retention", headers=JSON_HEADERS)
    assert response.status_code == 200
    assert response.get_json()["removed_by_table"]["audit_logs"] == 2


# ---------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------

def _emit(formatter, message, level=logging.INFO, **extra):
    record = logging.LogRecord(
        name="cloudpulse.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return formatter.format(record)


def test_json_formatter_emits_one_object_per_line():
    from services.logging_config import JSONFormatter

    line = _emit(
        JSONFormatter(),
        "health check finished",
        application="web-storefront",
        duration_ms=11.2,
    )

    payload = json.loads(line)
    assert payload["message"] == "health check finished"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "cloudpulse.test"
    assert payload["timestamp"]
    # Unrecognised attributes are nested under ``context``...
    assert payload["context"]["application"] == "web-storefront"
    # ...while known request fields are promoted to the top level.
    assert payload["duration_ms"] == 11.2


def test_json_formatter_omits_context_when_there_is_none():
    from services.logging_config import JSONFormatter

    payload = json.loads(_emit(JSONFormatter(), "plain message"))
    assert "context" not in payload


def test_json_formatter_promotes_request_context():
    from services.logging_config import JSONFormatter

    line = _emit(
        JSONFormatter(),
        "request handled",
        request_id="abc123",
        client_ip="10.0.0.1",
        status=200,
        duration_ms=12.5,
    )

    payload = json.loads(line)
    assert payload["request_id"] == "abc123"
    assert payload["client_ip"] == "10.0.0.1"
    assert payload["status"] == 200
    assert payload["duration_ms"] == 12.5


def test_json_formatter_includes_the_traceback():
    from services.logging_config import JSONFormatter

    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="cloudpulse.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(JSONFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


def test_text_formatter_keeps_the_pipe_layout():
    from services.logging_config import TextFormatter

    line = _emit(TextFormatter(), "cycle complete", status="UP")
    assert "INFO" in line
    assert "cloudpulse.test" in line
    assert "cycle complete" in line
    assert "status=UP" in line


def test_build_formatter_selects_the_requested_format():
    from services.logging_config import JSONFormatter, TextFormatter, build_formatter

    assert isinstance(build_formatter("json"), JSONFormatter)
    assert isinstance(build_formatter("text"), TextFormatter)
    # An unknown value must not crash the boot.
    assert isinstance(build_formatter("nonsense"), TextFormatter)


def test_configure_installs_a_single_handler(app):
    from services.logging_config import configure

    configure(app, log_format="json")
    first = len(logging.getLogger().handlers)

    configure(app, log_format="json")
    assert len(logging.getLogger().handlers) == first == 1


def test_logging_never_breaks_a_request(app, client):
    response = client.get("/health")
    assert response.status_code == 200


def test_request_id_is_returned_in_the_header(client):
    response = client.get("/health")
    assert response.headers.get("X-Request-ID")


def test_upstream_request_id_is_preserved(client):
    response = client.get("/health", headers={"X-Request-ID": "gateway-abc-123"})
    assert response.headers.get("X-Request-ID") == "gateway-abc-123"


def test_request_id_is_unique_per_request(client):
    first = client.get("/health").headers.get("X-Request-ID")
    second = client.get("/health").headers.get("X-Request-ID")
    assert first != second


def test_security_headers_are_present(client):
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
