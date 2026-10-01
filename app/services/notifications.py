"""Outbound alerting for incidents and deployments.

Delivery is intentionally best effort and isolated from the request path: a
failing webhook or SMTP server records a failed :class:`Notification` row but
never raises, so monitoring keeps working even when the alerting stack is down.
"""

from __future__ import annotations

import json
import logging
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from urllib.error import URLError
from urllib.request import Request, urlopen

from extensions import db
from flask import current_app
from models import Incident, Notification

logger = logging.getLogger("cloudpulse.notifications")

WEBHOOK_TIMEOUT = 5


def _setting(name: str, default=None):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def _now() -> datetime:
    return datetime.now(UTC)


def build_payload(event: str, incident: Incident) -> dict:
    application = incident.application
    return {
        "event": event,
        "source": "CloudPulse",
        "timestamp": _now().isoformat(),
        "incident": {
            "id": incident.id,
            "title": incident.title,
            "severity": incident.severity,
            "status": incident.status,
            "description": incident.description,
            "detected_at": (
                incident.detected_at.isoformat() if incident.detected_at else None
            ),
            "resolved_at": (
                incident.resolved_at.isoformat() if incident.resolved_at else None
            ),
        },
        "application": {
            "id": application.id if application else incident.application_id,
            "name": application.name if application else None,
            "url": application.url if application else None,
            "environment": application.environment if application else None,
        },
    }


def _record(
    event: str,
    channel: str,
    status: str,
    *,
    incident: Incident | None,
    target: str | None,
    error: str | None = None,
) -> None:
    try:
        db.session.add(
            Notification(
                incident_id=incident.id if incident else None,
                application_id=(
                    incident.application_id if incident else None
                ),
                channel=channel,
                target=target,
                event=event,
                status=status,
                attempts=1,
                last_error=error,
                sent_at=_now() if status == "sent" else None,
            )
        )
        db.session.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to record notification | Event: %s", event)
        db.session.rollback()


def _send_webhook(url: str, secret: str | None, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _setting("HEALTH_CHECK_USER_AGENT", "CloudPulse-Monitor/1.0"),
    }
    if secret:
        headers["X-CloudPulse-Signature"] = secret

    request = Request(url, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=WEBHOOK_TIMEOUT) as response:
        if response.status >= 300:
            raise RuntimeError(f"Webhook returned HTTP {response.status}")


def _send_email(recipients: list[str], subject: str, payload: dict) -> None:
    _deliver_email(recipients, subject, json.dumps(payload, indent=2))


def _deliver_email(recipients: list[str], subject: str, body: str) -> None:
    host = _setting("NOTIFY_SMTP_HOST")
    if not host:
        raise RuntimeError("NOTIFY_SMTP_HOST is not configured")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _setting("NOTIFY_SMTP_FROM", "cloudpulse@localhost")
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    with smtplib.SMTP(host, int(_setting("NOTIFY_SMTP_PORT", 587)), timeout=WEBHOOK_TIMEOUT) as smtp:
        if _setting("NOTIFY_SMTP_USE_TLS", True):
            smtp.starttls()
        user = _setting("NOTIFY_SMTP_USER")
        password = _setting("NOTIFY_SMTP_PASSWORD")
        if user and password:
            smtp.login(user, password)
        smtp.send_message(message)


def send_password_reset_email(user, link: str) -> bool:
    """Deliver a password reset link.

    Returns ``False`` when SMTP is not configured so the caller can fall back to
    logging the link instead of silently failing the recovery flow.
    """
    if not _setting("NOTIFY_SMTP_HOST"):
        return False

    recipients = _setting("NOTIFY_SMTP_TO") or [user.email]
    body = (
        "A password reset was requested for your CloudPulse account.\n\n"
        f"{link}\n\n"
        "This link expires in "
        f"{int(_setting('PASSWORD_RESET_TOKEN_TTL_SECONDS', 3600)) // 60} minutes "
        "and can be used once.\n\n"
        "If you did not request this, no action is needed: your password has "
        "not changed."
    )

    try:
        _deliver_email(recipients, "Reset your CloudPulse password", body)
    except (smtplib.SMTPException, OSError, RuntimeError) as error:
        logger.warning("Password reset email failed | Error: %s", error)
        return False

    _record(
        "password.reset_requested",
        "email",
        "sent",
        incident=None,
        target=user.email,
    )
    return True


def send_verification_email(user, link: str) -> bool:
    if not _setting("NOTIFY_SMTP_HOST"):
        return False

    recipients = _setting("NOTIFY_SMTP_TO") or [user.email]
    body = (
        "Welcome to CloudPulse.\n\n"
        f"Confirm your email address: {link}\n\n"
        "This link expires in "
        f"{int(_setting('EMAIL_VERIFICATION_TOKEN_TTL_SECONDS', 86400)) // 3600} hours."
    )

    try:
        _deliver_email(recipients, "Verify your CloudPulse email", body)
    except (smtplib.SMTPException, OSError, RuntimeError) as error:
        logger.warning("Verification email failed | Error: %s", error)
        return False

    _record(
        "email.verification_sent",
        "email",
        "sent",
        incident=None,
        target=user.email,
    )
    return True


def _deliver(event: str, incident: Incident) -> None:
    payload = build_payload(event, incident)
    subject = f"[CloudPulse][{incident.severity}] {incident.title}"
    application = incident.application
    app_label = application.name if application else f"app-{incident.application_id}"

    webhook_url = _setting("NOTIFY_WEBHOOK_URL")
    if webhook_url:
        try:
            _send_webhook(webhook_url, _setting("NOTIFY_WEBHOOK_SECRET"), payload)
            _record(event, "webhook", "sent", incident=incident, target=webhook_url)
            logger.info("Webhook delivered | Event: %s | Incident: %s", event, incident.id)
        except (URLError, OSError, RuntimeError, ValueError) as error:
            logger.warning("Webhook delivery failed | Error: %s", error)
            _record(event, "webhook", "failed", incident=incident, target=webhook_url, error=str(error))

    recipients = list(_setting("NOTIFY_SMTP_TO") or [])
    if recipients and _setting("NOTIFY_SMTP_HOST"):
        try:
            _send_email(recipients, subject, payload)
            _record(event, "email", "sent", incident=incident, target=", ".join(recipients))
            logger.info("Email delivered | Event: %s | Incident: %s", event, incident.id)
        except (smtplib.SMTPException, OSError, RuntimeError) as error:
            logger.warning("Email delivery failed | Error: %s", error)
            _record(event, "email", "failed", incident=incident,
                    target=", ".join(recipients), error=str(error))

    if not webhook_url and not recipients:
        _record(event, "log", "skipped", incident=incident, target=None)

    logger.info("Notification dispatch complete | Event: %s | App: %s", event, app_label)


def notify_incident_opened(incident: Incident) -> None:
    if not _setting("NOTIFY_ON_INCIDENT_OPEN", True):
        return
    _deliver("incident.opened", incident)


def notify_incident_resolved(incident: Incident) -> None:
    if not _setting("NOTIFY_ON_INCIDENT_RESOLVED", True):
        return
    _deliver("incident.resolved", incident)


def notify_deployment(deployment) -> None:
    if not _setting("NOTIFY_ON_DEPLOYMENT", True):
        return

    webhook_url = _setting("NOTIFY_WEBHOOK_URL")
    if not webhook_url:
        return

    payload = {
        "event": "deployment.recorded",
        "source": "CloudPulse",
        "timestamp": _now().isoformat(),
        "deployment": {
            "id": deployment.id,
            "application": (
                deployment.application.name if deployment.application else None
            ),
            "version": deployment.version,
            "environment": deployment.environment,
            "status": deployment.status,
            "deployed_at": (
                deployment.deployed_at.isoformat() if deployment.deployed_at else None
            ),
        },
    }

    try:
        _send_webhook(webhook_url, _setting("NOTIFY_WEBHOOK_SECRET"), payload)
        _record("deployment.recorded", "webhook", "sent", incident=None, target=webhook_url)
    except (URLError, OSError, RuntimeError, ValueError) as error:
        logger.warning("Deployment notification failed | Error: %s", error)
        _record("deployment.recorded", "webhook", "failed", incident=None,
                target=webhook_url, error=str(error))


def redeliver(event: str, incident: Incident) -> bool:
    """Re-attempt delivery of an already recorded event.

    Used by the operator triggered retry endpoint. Delivery is best effort, so a
    failure records another outbox row and returns normally; the caller reads the
    outcome from the outbox rather than from a return value.

    The ``NOTIFY_ON_*`` switches are honoured, exactly as they are on the first
    attempt. An operator who has turned incident alerts off to stop a noisy
    integration must not be able to re-enable them one row at a time by
    clicking retry.
    """
    if not _switch_allows(event):
        logger.info("Retry suppressed for %s by the notification switches", event)
        return False

    _deliver(event, incident)
    return True


def _switch_allows(event: str) -> bool:
    """Apply the same NOTIFY_ON_* gate the original send path uses."""
    if event == "incident.opened":
        return _setting("NOTIFY_ON_INCIDENT_OPEN", True)
    if event == "incident.resolved":
        return _setting("NOTIFY_ON_INCIDENT_RESOLVED", True)
    if event == "deployment.recorded":
        return _setting("NOTIFY_ON_DEPLOYMENT", True)
    return True
