"""Monitoring control plane endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from extensions import db
from flask import Blueprint, current_app, jsonify, request
from models import Application, AuditLog, AuthToken, HealthCheck, Notification
from security import login_required
from serializers import health_check_to_dict
from services import audit, metrics, retention
from services import health as health_service
from services import incidents as incident_service
from sqlalchemy import func, select

from api.helpers import (
    envelope,
    int_param,
    json_error,
    pagination_args,
    success,
    wants_pagination,
    write_required,
)

logger = logging.getLogger("cloudpulse.api.monitoring")

blueprint = Blueprint("monitoring_api", __name__, url_prefix="/api/monitor")


@blueprint.route("", methods=["GET"])
@blueprint.route("/", methods=["GET"])
@write_required
def monitor_all_applications():
    """Run a live monitoring pass over every application.

    Kept at the original path for backwards compatibility. Prefer
    ``POST /api/monitor/run`` or ``GET /api/monitor/summary`` for new clients.

    This writes rows, opens incidents and fires notifications, so it requires a
    write role. It previously accepted any authenticated user through a plain
    GET, which meant a read-only ``Viewer`` could trigger it, and so could any
    HTTP cache, link prefetcher or ``<img>`` tag that followed a link here --
    safe methods deliberately bypass the cross-origin mutation guard.
    """
    result = health_service.run_monitoring_cycle(force=True)
    metrics.mark_monitoring_run()
    return jsonify(result)


@blueprint.route("/run", methods=["POST"])
@write_required
def run_monitoring():
    force = request.args.get("force", "true").lower() not in {"0", "false"}
    result = health_service.run_monitoring_cycle(force=force)
    metrics.mark_monitoring_run()

    audit.record(
        "monitoring.cycle_run",
        entity_type="monitoring",
        summary=(
            f"Monitoring cycle checked {result['summary']['total']} applications "
            f"({result['summary']['unhealthy']} down)"
        ),
        detail=result["summary"],
    )

    return jsonify(result)


@blueprint.route("/summary", methods=["GET"])
@login_required
def monitoring_summary():
    """Cheap, side-effect free view of the current monitoring state."""
    total = db.session.scalar(select(func.count(Application.id))) or 0
    paused = (
        db.session.scalar(
            select(func.count(Application.id)).where(Application.is_paused.is_(True))
        )
        or 0
    )

    status_rows = dict(
        db.session.execute(
            select(Application.last_health_status, func.count(Application.id))
            .group_by(Application.last_health_status)
        ).all()
    )

    response_times = [
        check.response_time
        for check in db.session.scalars(
            select(HealthCheck)
            .where(
                HealthCheck.response_time.is_not(None),
                HealthCheck.checked_at
                >= datetime.now(UTC) - timedelta(hours=1),
            )
            .order_by(HealthCheck.checked_at.desc())
            .limit(500)
        )
    ]

    return jsonify(
        {
            "total_applications": total,
            "active_applications": total - paused,
            "paused_applications": paused,
            "healthy": status_rows.get("UP", 0),
            "degraded": status_rows.get("DEGRADED", 0),
            "unhealthy": status_rows.get("DOWN", 0),
            "unknown": status_rows.get("UNKNOWN", 0),
            "never_checked": (
                db.session.scalar(
                    select(func.count(Application.id)).where(
                        Application.last_checked_at.is_(None)
                    )
                )
                or 0
            ),
            "open_incidents": incident_service.count_open(),
            "sla_breaches": len(incident_service.breached_incidents()),
            "avg_response_time_ms": (
                round(sum(response_times) / len(response_times), 2)
                if response_times
                else None
            ),
            "last_checked_at": (
                db.session.scalar(select(func.max(HealthCheck.checked_at)))
            ),
        }
    )


@blueprint.route("/history", methods=["GET"])
@login_required
def monitoring_history():
    hours = int_param("hours", 24, minimum=1, maximum=24 * 30) or 24
    application_id = int_param("application_id")

    since = datetime.now(UTC) - timedelta(hours=hours)

    statement = select(HealthCheck).where(HealthCheck.checked_at >= since)
    if application_id:
        statement = statement.where(HealthCheck.application_id == application_id)

    total = db.session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    page, per_page, offset = pagination_args()
    statement = statement.order_by(HealthCheck.checked_at.desc())
    rows = list(db.session.scalars(statement.offset(offset).limit(per_page)))

    payload = [health_check_to_dict(row) for row in rows]

    if wants_pagination():
        return jsonify(envelope(payload, total, page, per_page, hours=hours))

    return jsonify(payload)


@blueprint.route("/uptime", methods=["GET"])
@login_required
def fleet_uptime():
    days = int_param("days", 7, minimum=1, maximum=90) or 7
    reports = []
    for application in db.session.scalars(select(Application).order_by(Application.id)):
        reports.append(health_service.uptime_report(application, days))
    return jsonify({"days": days, "applications": reports})


@blueprint.route("/retention", methods=["POST"])
@write_required
def prune_retention():
    days = int_param("days", None, minimum=0, maximum=3650)

    try:
        if days is None:
            # No explicit window: run every retention job at its configured age.
            summary = retention.prune_all()
        else:
            summary = {
                "health_checks": health_service.prune_health_checks(days),
                "audit_logs": retention.prune_audit_logs(days),
                "notifications": retention.prune_notifications(days),
            }
            db.session.commit()
    except retention.RetentionError as error:
        # A failure must not look like a successful prune of nothing, or the
        # operator concludes the backlog is clear while it keeps growing.
        return json_error(
            "Retention job failed; no rows were removed", 500, detail=str(error)
        )

    audit.record(
        "monitoring.retention_pruned",
        entity_type="monitoring",
        summary=(
            "Pruned history: "
            + ", ".join(f"{value} {name}" for name, value in summary.items())
        ),
        detail=summary,
    )

    return success(
        f"Pruned {sum(summary.values())} records",
        removed=sum(summary.values()),
        removed_by_table=summary,
        retention_days=days,
    )


@blueprint.route("/retention", methods=["GET"])
@login_required
def retention_status():
    """Report how much history is stored and when it will be pruned."""
    now = datetime.now(UTC)
    counts = {
        "health_checks": db.session.scalar(select(func.count(HealthCheck.id))) or 0,
        "audit_logs": db.session.scalar(select(func.count(AuditLog.id))) or 0,
        "notifications": db.session.scalar(select(func.count(Notification.id))) or 0,
        "auth_tokens": db.session.scalar(select(func.count(AuthToken.id))) or 0,
    }
    oldest = {
        "health_checks": db.session.scalar(select(func.min(HealthCheck.checked_at))),
        "audit_logs": db.session.scalar(select(func.min(AuditLog.created_at))),
    }

    policies = {
        "health_checks_days": current_app.config.get("HEALTH_CHECK_RETENTION_DAYS"),
        "audit_logs_days": current_app.config.get("AUDIT_LOG_RETENTION_DAYS"),
        "notifications_days": current_app.config.get("NOTIFICATION_RETENTION_DAYS"),
        "job_interval_hours": current_app.config.get("RETENTION_JOB_HOURS"),
    }

    return jsonify(
        {
            "counts": counts,
            "oldest_record": {
                name: (value.isoformat() if value else None)
                for name, value in oldest.items()
            },
            "policies": policies,
            "checked_at": now.isoformat(),
        }
    )


@blueprint.route("/scheduler", methods=["GET"])
@login_required
def scheduler_status():
    from services import scheduler

    return jsonify(scheduler.status())
