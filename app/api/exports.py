"""CSV export endpoints for offline reporting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from extensions import db
from flask import Blueprint, request
from models import Application, AuditLog, Deployment, HealthCheck, Incident
from security import login_required
from sqlalchemy import select

from api.helpers import csv_response, int_param, json_error

blueprint = Blueprint("exports_api", __name__, url_prefix="/api/export")

APPLICATION_HEADERS = [
    "id",
    "name",
    "url",
    "environment",
    "status",
    "last_health_status",
    "last_http_status",
    "last_response_time",
    "uptime_percentage",
    "open_incidents",
    "created_at",
]

INCIDENT_HEADERS = [
    "id",
    "application",
    "title",
    "severity",
    "status",
    "source",
    "assignee",
    "detected_at",
    "resolved_at",
    "duration_minutes",
]

DEPLOYMENT_HEADERS = [
    "id",
    "application",
    "version",
    "environment",
    "status",
    "commit_hash",
    "deployed_by",
    "duration_seconds",
    "deployed_at",
]

HEALTH_CHECK_HEADERS = [
    "id",
    "application_id",
    "status",
    "http_status",
    "response_time",
    "checked_at",
    "error_message",
]


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _iso(value) -> str:
    return value.isoformat() if value else ""


@blueprint.route("/applications", methods=["GET"])
@login_required
def export_applications():
    rows = list(db.session.scalars(select(Application).order_by(Application.id)))
    data = [
        [
            application.id,
            application.name,
            application.url,
            application.environment,
            application.status,
            application.last_health_status or "",
            application.last_http_status or "",
            application.last_response_time or "",
            application.uptime_percentage,
            sum(1 for incident in application.incidents if incident.status != "Resolved"),
            _iso(application.created_at),
        ]
        for application in rows
    ]
    return csv_response(f"cloudpulse-applications-{_stamp()}.csv", APPLICATION_HEADERS, data)


@blueprint.route("/incidents", methods=["GET"])
@login_required
def export_incidents():
    days = int_param("days", 90, minimum=1, maximum=3650) or 90
    status = request.args.get("status")

    statement = select(Incident).order_by(Incident.detected_at.desc())
    if status:
        statement = statement.where(Incident.status == status)
    statement = statement.where(
        Incident.detected_at >= datetime.now(UTC) - timedelta(days=days)
    )

    rows = list(db.session.scalars(statement))
    data = [
        [
            incident.id,
            incident.application.name if incident.application else "",
            incident.title,
            incident.severity,
            incident.status,
            incident.source,
            incident.assignee or "",
            _iso(incident.detected_at),
            _iso(incident.resolved_at),
            incident.detection_to_resolution_minutes or "",
        ]
        for incident in rows
    ]
    return csv_response(f"cloudpulse-incidents-{_stamp()}.csv", INCIDENT_HEADERS, data)


@blueprint.route("/deployments", methods=["GET"])
@login_required
def export_deployments():
    rows = list(db.session.scalars(select(Deployment).order_by(Deployment.deployed_at.desc())))
    data = [
        [
            deployment.id,
            deployment.application.name if deployment.application else "",
            deployment.version,
            deployment.environment,
            deployment.status,
            deployment.commit_hash or "",
            deployment.deployed_by or "",
            deployment.duration_seconds or "",
            _iso(deployment.deployed_at),
        ]
        for deployment in rows
    ]
    return csv_response(
        f"cloudpulse-deployments-{_stamp()}.csv", DEPLOYMENT_HEADERS, data
    )


@blueprint.route("/health-checks", methods=["GET"])
@login_required
def export_health_checks():
    application_id = int_param("application_id")
    limit = int_param("limit", 1000, minimum=1, maximum=50000) or 1000

    statement = select(HealthCheck).order_by(HealthCheck.checked_at.desc())
    if application_id:
        statement = statement.where(HealthCheck.application_id == application_id)

    rows = list(db.session.scalars(statement.limit(limit)))
    data = [
        [
            check.id,
            check.application_id,
            check.status,
            check.http_status or "",
            check.response_time or "",
            _iso(check.checked_at),
            (check.error_message or "").replace("\n", " "),
        ]
        for check in rows
    ]
    return csv_response(
        f"cloudpulse-health-checks-{_stamp()}.csv", HEALTH_CHECK_HEADERS, data
    )


@blueprint.route("/activity", methods=["GET"])
@login_required
def export_activity():
    limit = int_param("limit", 1000, minimum=1, maximum=20000) or 1000
    rows = list(
        db.session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
    )
    data = [
        [
            row.id,
            _iso(row.created_at),
            row.actor_name or "system",
            row.action,
            row.entity_type or "",
            row.entity_id or "",
            row.summary or "",
        ]
        for row in rows
    ]
    return csv_response(
        f"cloudpulse-activity-{_stamp()}.csv",
        ["id", "created_at", "actor", "action", "entity_type", "entity_id", "summary"],
        data,
    )


@blueprint.route("", methods=["GET"])
def index():
    return json_error("Specify a resource to export", 400)
