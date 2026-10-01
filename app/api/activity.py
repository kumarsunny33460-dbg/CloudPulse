"""Activity timeline and audit trail endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from extensions import db
from flask import Blueprint, current_app, jsonify, request
from models import AuditLog, Notification
from security import login_required
from serializers import audit_log_to_dict
from sqlalchemy import func, or_, select

from api.helpers import (
    envelope,
    int_param,
    json_error,
    pagination_args,
    success,
    wants_pagination,
    write_required,
)

blueprint = Blueprint("audit_api", __name__, url_prefix="/api/activity")

SORT_COLUMNS = {
    "id": AuditLog.id,
    "created_at": AuditLog.created_at,
    "action": AuditLog.action,
}


@blueprint.route("", methods=["GET"])
@blueprint.route("/", methods=["GET"])
@login_required
def activity():
    statement = select(AuditLog)

    search = (request.args.get("search") or "").strip()
    if search:
        pattern = f"%{search}%"
        statement = statement.where(
            or_(
                AuditLog.summary.ilike(pattern),
                AuditLog.action.ilike(pattern),
                AuditLog.actor_name.ilike(pattern),
            )
        )

    entity_type = request.args.get("entity_type")
    if entity_type:
        statement = statement.where(AuditLog.entity_type == entity_type)

    # A filter that is present in the query string must always be applied. Testing
    # the parsed value instead dropped the clause for ``entity_id=0`` and for
    # anything unparseable, so a caller asking for one entity silently received
    # the whole audit trail.
    for name, column in (
        ("entity_id", AuditLog.entity_id),
        ("actor_id", AuditLog.actor_id),
    ):
        if request.args.get(name) is None:
            continue
        value = int_param(name)
        if value is None:
            return json_error(f"{name} must be an integer", 422)
        statement = statement.where(column == value)

    sort = request.args.get("sort", "created_at")
    column = SORT_COLUMNS.get(sort, AuditLog.created_at)
    order = request.args.get("order", "desc").lower()
    statement = statement.order_by(column.asc() if order == "asc" else column.desc())

    total = db.session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    page, per_page, offset = pagination_args()
    rows = list(db.session.scalars(statement.offset(offset).limit(per_page)))

    payload = [audit_log_to_dict(row) for row in rows]

    if wants_pagination():
        return jsonify(envelope(payload, total, page, per_page))

    return jsonify(payload)


# ---------------------------------------------------------------------
# Notification outbox
# ---------------------------------------------------------------------

@blueprint.route("/notifications", methods=["GET"])
@login_required
def notifications():
    """Recent delivery attempts, newest first.

    The outbox is the audit trail for alerting: it records what was sent, on
    which channel, and why a delivery failed, so a silent notification gap is
    diagnosable without reading the application log.
    """
    from serializers import notification_to_dict

    statement = select(Notification)

    status = request.args.get("status")
    if status:
        statement = statement.where(Notification.status == status)

    channel = request.args.get("channel")
    if channel:
        statement = statement.where(Notification.channel == channel)

    event = request.args.get("event")
    if event:
        statement = statement.where(Notification.event == event)

    incident_id = int_param("incident_id")
    if incident_id:
        statement = statement.where(Notification.incident_id == incident_id)

    total = db.session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    page, per_page, offset = pagination_args()
    rows = list(
        db.session.scalars(
            statement.order_by(Notification.created_at.desc(), Notification.id.desc())
            .offset(offset)
            .limit(per_page)
        )
    )

    payload = [notification_to_dict(row) for row in rows]

    if wants_pagination():
        return jsonify(envelope(payload, total, page, per_page))

    return jsonify(payload)


@blueprint.route("/notifications/summary", methods=["GET"])
@login_required
def notification_summary():
    """Delivery health per channel plus the alerting configuration in force."""
    by_status = dict(
        db.session.execute(
            select(Notification.status, func.count(Notification.id))
            .group_by(Notification.status)
        ).all()
    )

    sent = by_status.get("sent", 0)
    failed = by_status.get("failed", 0)
    # A retried notification keeps its original row for the audit trail but
    # marks it superseded, so it is counted once on its final outcome rather
    # than as a failure forever.
    superseded = by_status.get("superseded", 0)
    attempted = sent + failed

    configuration = {
        "webhook_configured": bool(current_app.config.get("NOTIFY_WEBHOOK_URL")),
        "smtp_configured": bool(current_app.config.get("NOTIFY_SMTP_HOST")),
        "on_incident_open": current_app.config.get("NOTIFY_ON_INCIDENT_OPEN"),
        "on_incident_resolved": current_app.config.get("NOTIFY_ON_INCIDENT_RESOLVED"),
        "on_deployment": current_app.config.get("NOTIFY_ON_DEPLOYMENT"),
    }

    return jsonify(
        {
            "by_status": by_status,
            "superseded": superseded,
            "success_rate": (
                round((sent / attempted) * 100, 2) if attempted else None
            ),
            "configuration": configuration,
            "retention_days": current_app.config.get("NOTIFICATION_RETENTION_DAYS"),
        }
    )


@blueprint.route("/notifications/<int:notification_id>/retry", methods=["POST"])
@write_required
def retry_notification(notification_id: int):
    """Re-attempt delivery of one failed notification."""
    from models import Incident
    from serializers import notification_to_dict
    from services import audit
    from services import notifications as notification_service

    notification = db.session.get(Notification, notification_id)
    if notification is None:
        return jsonify({"error": "Notification not found"}), 404

    if notification.status == "sent":
        return jsonify({"error": "Notification already delivered"}), 409

    # Bound how far back a retry may reach. Without this, a single retained row
    # from years ago can still fire a webhook, which turns the outbox into an
    # unbounded replay queue for whoever can reach the endpoint.
    max_age_days = int(current_app.config.get("NOTIFICATION_RETRY_MAX_AGE_DAYS", 7))
    if max_age_days > 0 and notification.created_at is not None:
        created = notification.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_days = (datetime.now(UTC) - created).total_seconds() / 86400
        if age_days > max_age_days:
            return jsonify({
                "error": (
                    f"This notification is {age_days:.0f} days old; retries are "
                    f"limited to {max_age_days} days."
                )
            }), 409

    incident = (
        db.session.get(Incident, notification.incident_id)
        if notification.incident_id
        else None
    )
    if incident is None:
        return jsonify({
            "error": "The incident behind this notification no longer exists"
        }), 409

    notification.attempts = (notification.attempts or 0) + 1
    db.session.commit()

    # ``_deliver`` records a fresh outbox row so the attempt history is kept,
    # which left this row permanently ``failed``: the success rate counted it
    # forever and the failure list never cleared, even after the retry actually
    # delivered. Mark the original as superseded so the summary counts each
    # notification once, on its final outcome, while the row itself remains as
    # the audit trail of what happened.
    notification.status = "superseded"
    notification.last_error = None
    db.session.commit()

    notification_service.redeliver(notification.event, incident)

    audit.record(
        "notification.retried",
        entity_type="notification",
        entity_id=notification.id,
        summary=f"Retried {notification.channel} notification for {notification.event}",
        detail={"attempts": notification.attempts, "event": notification.event},
    )

    replacement = (
        db.session.query(Notification)
        .filter(Notification.incident_id == notification.incident_id)
        .filter(Notification.event == notification.event)
        .order_by(Notification.id.desc())
        .first()
    )

    return success(
        "Notification retried",
        notification=notification_to_dict(
            replacement if replacement is not None else notification
        ),
        superseded=notification_to_dict(notification),
    )
