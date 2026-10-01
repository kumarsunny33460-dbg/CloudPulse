"""Tests for the notification outbox API and delivery behaviour."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from conftest import JSON_HEADERS

# ---------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------

class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_webhook_is_delivered_and_recorded(app, make_incident, session):
    from models import Notification
    from services import notifications

    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/cloudpulse"

    incident = make_incident()

    with patch("services.notifications.urlopen", return_value=_FakeResponse()):
        notifications.notify_incident_opened(incident)

    row = session.query(Notification).order_by(Notification.id.desc()).first()
    assert row.channel == "webhook"
    assert row.status == "sent"
    assert row.event == "incident.opened"
    assert row.sent_at is not None


def test_webhook_failure_is_recorded_not_raised(app, make_incident, session):
    from models import Notification
    from services import notifications

    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/cloudpulse"

    incident = make_incident()

    with patch("services.notifications.urlopen", side_effect=OSError("connection refused")):
        notifications.notify_incident_opened(incident)  # must not raise

    row = session.query(Notification).order_by(Notification.id.desc()).first()
    assert row.status == "failed"
    assert "connection refused" in row.last_error


def test_webhook_payload_carries_the_incident(app, make_incident):
    from services import notifications

    incident = make_incident(severity="Critical", title="Payments down")
    payload = notifications.build_payload("incident.opened", incident)

    assert payload["source"] == "CloudPulse"
    assert payload["event"] == "incident.opened"
    assert payload["incident"]["severity"] == "Critical"
    assert payload["incident"]["title"] == "Payments down"
    assert payload["application"]["id"] == incident.application_id


def test_no_channel_configured_records_a_skip(app, make_incident, session):
    from models import Notification
    from services import notifications

    app.config["NOTIFY_WEBHOOK_URL"] = None
    app.config["NOTIFY_SMTP_HOST"] = None
    app.config["NOTIFY_SMTP_TO"] = []

    incident = make_incident()
    notifications.notify_incident_opened(incident)

    row = session.query(Notification).order_by(Notification.id.desc()).first()
    assert row.channel == "log"
    assert row.status == "skipped"


def test_incident_open_notification_can_be_disabled(app, make_incident, session):
    from models import Notification
    from services import notifications

    app.config["NOTIFY_ON_INCIDENT_OPEN"] = False
    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/cloudpulse"

    incident = make_incident()
    notifications.notify_incident_opened(incident)

    assert session.query(Notification).count() == 0


# ---------------------------------------------------------------------
# Outbox API
# ---------------------------------------------------------------------

def test_notifications_endpoint_requires_login(app, client):
    response = client.get("/api/activity/notifications", headers=JSON_HEADERS)
    assert response.status_code == 401


def test_notifications_endpoint_lists_the_outbox(app, admin_client, make_incident, session):
    from models import Notification

    incident = make_incident()
    session.add(
        Notification(
            incident_id=incident.id,
            application_id=incident.application_id,
            channel="webhook",
            event="incident.opened",
            status="sent",
            sent_at=datetime.now(UTC),
        )
    )
    session.commit()

    response = admin_client.get("/api/activity/notifications", headers=JSON_HEADERS)
    assert response.status_code == 200

    payload = response.get_json()
    assert len(payload) == 1
    assert payload[0]["channel"] == "webhook"
    assert payload[0]["status"] == "sent"


def test_notifications_can_be_filtered_by_status(
    app, admin_client, make_incident, session
):
    from models import Notification

    incident = make_incident()
    for status in ("sent", "failed", "sent"):
        session.add(
            Notification(
                incident_id=incident.id,
                application_id=incident.application_id,
                channel="webhook",
                event="incident.opened",
                status=status,
            )
        )
    session.commit()

    response = admin_client.get(
        "/api/activity/notifications?status=failed", headers=JSON_HEADERS
    )
    payload = response.get_json()
    assert len(payload) == 1
    assert payload[0]["status"] == "failed"


def test_notifications_support_pagination(app, admin_client, make_incident, session):
    from models import Notification

    incident = make_incident()
    for index in range(7):
        session.add(
            Notification(
                incident_id=incident.id,
                application_id=incident.application_id,
                channel="log",
                event=f"event.{index}",
                status="skipped",
            )
        )
    session.commit()

    response = admin_client.get(
        "/api/activity/notifications?page=1&per_page=3", headers=JSON_HEADERS
    )
    payload = response.get_json()

    assert len(payload["data"]) == 3
    assert payload["pagination"]["total"] == 7
    assert payload["pagination"]["total_pages"] == 3


def test_notification_summary_reports_success_rate(
    app, admin_client, make_incident, session
):
    from models import Notification

    incident = make_incident()
    for status in ("sent", "sent", "sent", "failed"):
        session.add(
            Notification(
                incident_id=incident.id,
                application_id=incident.application_id,
                channel="webhook",
                event="incident.opened",
                status=status,
            )
        )
    session.commit()

    response = admin_client.get(
        "/api/activity/notifications/summary", headers=JSON_HEADERS
    )
    assert response.status_code == 200

    payload = response.get_json()
    assert payload["by_status"]["sent"] == 3
    assert payload["by_status"]["failed"] == 1
    assert payload["success_rate"] == 75.0


def test_notification_summary_with_no_history(app, admin_client):
    payload = admin_client.get(
        "/api/activity/notifications/summary", headers=JSON_HEADERS
    ).get_json()

    assert payload["by_status"] == {}
    assert payload["success_rate"] is None


def test_notification_summary_exposes_configuration(app, admin_client):
    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/x"
    app.config["NOTIFY_SMTP_HOST"] = "smtp.example.com"

    payload = admin_client.get(
        "/api/activity/notifications/summary", headers=JSON_HEADERS
    ).get_json()

    assert payload["configuration"]["webhook_configured"] is True
    assert payload["configuration"]["smtp_configured"] is True


# ---------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------

def test_retry_creates_a_fresh_delivery_attempt(
    app, admin_client, make_incident, session
):
    from models import Notification

    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/cloudpulse"

    incident = make_incident()
    failed = Notification(
        incident_id=incident.id,
        application_id=incident.application_id,
        channel="webhook",
        event="incident.opened",
        status="failed",
        attempts=1,
        last_error="connection refused",
    )
    session.add(failed)
    session.commit()

    with patch("services.notifications.urlopen", return_value=_FakeResponse()):
        response = admin_client.post(
            f"/api/activity/notifications/{failed.id}/retry", headers=JSON_HEADERS
        )

    assert response.status_code == 200

    payload = response.get_json()

    # The response reports the new delivery attempt, and separately identifies
    # the row it replaced so the caller can see what was superseded.
    assert payload["notification"]["status"] == "sent"
    assert payload["notification"]["sent_at"] is not None
    assert payload["superseded"]["attempts"] == 2
    assert payload["superseded"]["status"] == "superseded"

    # The original row is kept for the audit trail but no longer counts as a
    # failure, so the success rate is not permanently depressed by a retry that
    # actually worked.
    session.expire_all()
    assert session.query(Notification).count() == 2
    assert session.query(Notification).filter_by(status="sent").count() == 1
    assert session.query(Notification).filter_by(status="failed").count() == 0
    assert session.query(Notification).filter_by(status="superseded").count() == 1


def test_retried_failure_is_not_reported_in_the_success_rate(
    app, admin_client, make_incident, session
):
    """A retry that succeeds must not leave the channel looking unreliable."""
    from models import Notification

    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/cloudpulse"
    incident = make_incident()

    failed = Notification(
        incident_id=incident.id,
        application_id=incident.application_id,
        channel="webhook",
        event="incident.opened",
        status="failed",
        attempts=1,
    )
    session.add(failed)
    session.commit()

    with patch("services.notifications.urlopen", return_value=_FakeResponse()):
        admin_client.post(
            f"/api/activity/notifications/{failed.id}/retry", headers=JSON_HEADERS
        )

    summary = admin_client.get(
        "/api/activity/notifications/summary", headers=JSON_HEADERS
    ).get_json()

    assert summary["by_status"].get("failed", 0) == 0
    assert summary["superseded"] == 1
    assert summary["success_rate"] == 100.0

    # And the superseded row is still on record.
    assert session.query(Notification).filter_by(status="superseded").count() == 1


def test_retry_respects_the_alerting_kill_switches(
    app, admin_client, make_incident, session
):
    """Turning alerts off must also stop the retry button."""
    from models import Notification

    incident = make_incident()
    row = Notification(
        incident_id=incident.id,
        application_id=incident.application_id,
        channel="webhook",
        event="incident.opened",
        status="failed",
    )
    session.add(row)
    session.commit()

    app.config["NOTIFY_ON_INCIDENT_OPEN"] = False
    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/cloudpulse"

    with patch("services.notifications.urlopen") as deliver:
        response = admin_client.post(
            f"/api/activity/notifications/{row.id}/retry", headers=JSON_HEADERS
        )

    assert response.status_code == 200
    assert deliver.call_count == 0, "a disabled alert must not be re-sent"


def test_retry_of_an_ancient_notification_is_refused(
    app, admin_client, make_incident, session
):
    from datetime import UTC, datetime, timedelta

    from models import Notification

    incident = make_incident()
    row = Notification(
        incident_id=incident.id,
        application_id=incident.application_id,
        channel="webhook",
        event="incident.opened",
        status="failed",
        created_at=datetime.now(UTC) - timedelta(days=900),
    )
    session.add(row)
    session.commit()

    app.config["NOTIFICATION_RETRY_MAX_AGE_DAYS"] = 7

    response = admin_client.post(
        f"/api/activity/notifications/{row.id}/retry", headers=JSON_HEADERS
    )

    assert response.status_code == 409
    assert b"days old" in response.data


def test_retry_within_the_age_bound_still_works(
    app, admin_client, make_incident, session
):
    from datetime import UTC, datetime, timedelta

    from models import Notification

    incident = make_incident()
    row = Notification(
        incident_id=incident.id,
        application_id=incident.application_id,
        channel="webhook",
        event="incident.opened",
        status="failed",
        created_at=datetime.now(UTC) - timedelta(days=2),
    )
    session.add(row)
    session.commit()

    app.config["NOTIFICATION_RETRY_MAX_AGE_DAYS"] = 7
    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/cloudpulse"

    with patch("services.notifications.urlopen", return_value=_FakeResponse()):
        response = admin_client.post(
            f"/api/activity/notifications/{row.id}/retry", headers=JSON_HEADERS
        )

    assert response.status_code == 200


def test_retry_of_a_delivered_notification_is_refused(
    app, admin_client, make_incident, session
):
    from models import Notification

    incident = make_incident()
    row = Notification(
        incident_id=incident.id,
        application_id=incident.application_id,
        channel="webhook",
        event="incident.opened",
        status="sent",
        sent_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()

    response = admin_client.post(
        f"/api/activity/notifications/{row.id}/retry", headers=JSON_HEADERS
    )
    assert response.status_code == 409


def test_retry_of_an_unknown_notification_is_404(app, admin_client):
    response = admin_client.post(
        "/api/activity/notifications/99999/retry", headers=JSON_HEADERS
    )
    assert response.status_code == 404


def test_retry_requires_write_role(app, viewer_client, make_incident, session):
    from models import Notification

    incident = make_incident()
    row = Notification(
        incident_id=incident.id,
        application_id=incident.application_id,
        channel="webhook",
        event="incident.opened",
        status="failed",
    )
    session.add(row)
    session.commit()

    response = viewer_client.post(
        f"/api/activity/notifications/{row.id}/retry", headers=JSON_HEADERS
    )
    assert response.status_code == 403


def test_retry_without_an_incident_is_refused(app, admin_client, session):
    from models import Notification

    row = Notification(
        channel="webhook",
        event="incident.opened",
        status="failed",
    )
    session.add(row)
    session.commit()

    response = admin_client.post(
        f"/api/activity/notifications/{row.id}/retry", headers=JSON_HEADERS
    )
    assert response.status_code == 409


# ---------------------------------------------------------------------
# Password reset and verification email
# ---------------------------------------------------------------------

class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.sent = []
        self.started_tls = False
        self.logged_in = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent.append(message)


def test_password_reset_email_is_sent_when_smtp_is_configured(
    app, admin_client, session
):
    from models import User
    from services import accounts

    _FakeSMTP.instances.clear()
    app.config["NOTIFY_SMTP_HOST"] = "smtp.example.com"
    app.config["NOTIFY_SMTP_TO"] = []

    user = session.query(User).filter_by(email="admin@cloudpulse.test").one()
    token = accounts.issue_token(user, accounts.PASSWORD_RESET)

    with patch("services.notifications.smtplib.SMTP", _FakeSMTP):
        delivered = accounts.deliver_reset_link(user, token)

    assert delivered is True
    assert len(_FakeSMTP.instances) == 1

    message = _FakeSMTP.instances[0].sent[0]
    assert "Reset your CloudPulse" in message["Subject"]
    assert token in message.get_content()


def test_password_reset_link_is_logged_when_smtp_is_missing(
    app, admin_client, session, caplog
):
    from models import User
    from services import accounts

    app.config["NOTIFY_SMTP_HOST"] = None

    user = session.query(User).filter_by(email="admin@cloudpulse.test").one()
    token = accounts.issue_token(user, accounts.PASSWORD_RESET)

    with caplog.at_level("WARNING", logger="cloudpulse.accounts"):
        delivered = accounts.deliver_reset_link(user, token)

    assert delivered is False
    assert token in caplog.text


def test_verification_email_is_sent(app, admin_client, session):
    from models import User
    from services import accounts

    _FakeSMTP.instances.clear()
    app.config["NOTIFY_SMTP_HOST"] = "smtp.example.com"

    user = session.query(User).filter_by(email="admin@cloudpulse.test").one()
    token = accounts.issue_token(user, accounts.EMAIL_VERIFICATION)

    with patch("services.notifications.smtplib.SMTP", _FakeSMTP):
        delivered = accounts.deliver_verification_link(user, token)

    assert delivered is True
    assert "Verify your CloudPulse" in _FakeSMTP.instances[0].sent[0]["Subject"]


def test_smtp_failure_is_swallowed(app, admin_client, session):
    from models import User
    from services import accounts

    app.config["NOTIFY_SMTP_HOST"] = "smtp.example.com"

    user = session.query(User).filter_by(email="admin@cloudpulse.test").one()
    token = accounts.issue_token(user, accounts.PASSWORD_RESET)

    with patch(
        "services.notifications.smtplib.SMTP",
        side_effect=OSError("connection refused"),
    ):
        assert accounts.deliver_reset_link(user, token) is False


def test_reset_link_uses_the_configured_public_url(app, admin_client, session):
    from models import User
    from services import accounts

    _FakeSMTP.instances.clear()
    app.config["NOTIFY_SMTP_HOST"] = "smtp.example.com"
    app.config["PUBLIC_BASE_URL"] = "https://cloudpulse.example.com"

    user = session.query(User).filter_by(email="admin@cloudpulse.test").one()
    token = accounts.issue_token(user, accounts.PASSWORD_RESET)

    with patch("services.notifications.smtplib.SMTP", _FakeSMTP):
        accounts.deliver_reset_link(user, token)

    body = _FakeSMTP.instances[0].sent[0].get_content()
    assert "https://cloudpulse.example.com/reset-password/" in body


def test_emitted_links_actually_resolve(app, admin_client, session):
    """A recovery link built outside a request must still be a live route.

    The Flask endpoint name (``reset_password``) differs from the URL rule
    (``/reset-password/<token>``), so deriving one from the other produces a
    link that 404s and leaves the user unable to reset their password.
    """
    from models import User
    from services import accounts

    app.config["PUBLIC_BASE_URL"] = ""
    user = session.query(User).filter_by(email="admin@cloudpulse.test").one()

    rules = {rule.rule for rule in app.url_map.iter_rules()}

    reset_token = accounts.issue_token(user, accounts.PASSWORD_RESET)
    verify_token = accounts.issue_token(user, accounts.EMAIL_VERIFICATION)

    for link in (
        accounts._link_for(reset_token, accounts.PASSWORD_RESET),
        accounts._link_for(verify_token, accounts.EMAIL_VERIFICATION),
    ):
        # Strip the host and substitute the path parameter back out.
        path = link.split("://")[-1].split("/", 1)[-1]
        path = "/" + path if not path.startswith("/") else path
        route = path.rsplit("/", 1)[0] + "/<token>"

        assert route in rules, f"{link} does not match any registered route"

        # And following it must not 404.
        assert client_get(app, path).status_code == 200


def client_get(app, path):
    with app.test_client() as client:
        return client.get(path)


# ---------------------------------------------------------------------
# Deployment notifications
# ---------------------------------------------------------------------

def test_deployment_notification_is_delivered(app, make_deployment, session):
    from models import Notification
    from services import notifications

    app.config["NOTIFY_WEBHOOK_URL"] = "https://hooks.example.com/cloudpulse"

    deployment = make_deployment(version="v2.0.0")
    with patch("services.notifications.urlopen", return_value=_FakeResponse()):
        notifications.notify_deployment(deployment)

    row = session.query(Notification).order_by(Notification.id.desc()).first()
    assert row.event == "deployment.recorded"
    assert row.status == "sent"
