"""Aggregated dashboard endpoints powering the overview charts."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from extensions import db
from flask import Blueprint, jsonify
from models import Application, Deployment, HealthCheck, Incident
from security import login_required
from services import incidents as incident_service
from sqlalchemy import func, select

from api.helpers import int_param

blueprint = Blueprint("overview_api", __name__, url_prefix="/api/overview")


def _bucket_stamp(moment: datetime, hours: int) -> str:
    if hours <= 24:
        return moment.strftime("%Y-%m-%dT%H:00:00+00:00")
    return moment.strftime("%Y-%m-%d")


@blueprint.route("", methods=["GET"])
@login_required
def overview():
    """A single round trip that fills the entire dashboard header and charts."""
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

    open_incidents = incident_service.count_open()

    return jsonify(
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "applications": {
                "total": total,
                "active": total - paused,
                "paused": paused,
                "healthy": status_rows.get("UP", 0),
                "degraded": status_rows.get("DEGRADED", 0),
                "unhealthy": status_rows.get("DOWN", 0),
                "unknown": status_rows.get("UNKNOWN", 0),
                "never_checked": status_rows.get(None, 0),
                "by_environment": _applications_by_environment(),
            },
            "incidents": {
                "open": open_incidents,
                "total": db.session.scalar(select(func.count(Incident.id))) or 0,
                "by_severity": incident_service.severity_breakdown(),
                "by_status": incident_service.status_breakdown(),
            },
            "deployments": {
                "total": db.session.scalar(select(func.count(Deployment.id))) or 0,
                "by_status": _deployments_by_status(),
            },
            "reliability": incident_service.statistics(30),
        }
    )


@blueprint.route("/timeseries", methods=["GET"])
@login_required
def timeseries():
    hours = int_param("hours", 24, minimum=1, maximum=24 * 90) or 24

    since = datetime.now(UTC) - timedelta(hours=hours)
    checks = list(
        db.session.scalars(
            select(HealthCheck)
            .where(HealthCheck.checked_at >= since)
            .order_by(HealthCheck.checked_at.asc())
        )
    )

    step = timedelta(hours=1) if hours <= 48 else timedelta(days=1)
    buckets: Counter = Counter()
    latencies: dict[str, list[float]] = {}

    for check in checks:
        moment = check.checked_at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)

        offset = int((moment - since).total_seconds() // step.total_seconds())
        key = _bucket_stamp(since + step * offset, hours)

        buckets[key] += 1
        if check.status in ("UP", "DEGRADED"):
            latencies.setdefault(key + "::ok", []).append(check.response_time or 0.0)
        latencies.setdefault(key + "::fail", []).append(0.0 if check.status == "DOWN" else None)

    labels: list[str] = []
    total_points: list[int] = []
    avg_latency: list[float | None] = []
    failure_points: list[int] = []

    cursor = since
    while cursor < datetime.now(UTC):
        key = _bucket_stamp(cursor, hours)
        labels.append(key)
        total_points.append(buckets.get(key, 0))

        ok_values = [value for value in latencies.get(key + "::ok", []) if value is not None]
        avg_latency.append(round(sum(ok_values) / len(ok_values), 2) if ok_values else None)

        raw_fail = latencies.get(key + "::fail", [])
        failure_points.append(len([value for value in raw_fail if value == 0.0]))
        cursor += step

    return jsonify(
        {
            "hours": hours,
            "labels": labels,
            "checks": total_points,
            "avg_response_time_ms": avg_latency,
            "failures": failure_points,
        }
    )


@blueprint.route("/leaderboard", methods=["GET"])
@login_required
def leaderboard():
    limit = int_param("limit", 10, minimum=1, maximum=50) or 10
    rows = db.session.scalars(
        select(Application).order_by(Application.uptime_percentage.asc()).limit(limit)
    )
    return jsonify(
        [
            {
                "id": application.id,
                "name": application.name,
                "environment": application.environment,
                "uptime_percentage": application.uptime_percentage,
                "last_health_status": application.last_health_status,
                "consecutive_failures": application.consecutive_failures,
            }
            for application in rows
        ]
    )


def _applications_by_environment() -> list[dict]:
    rows = db.session.execute(
        select(Application.environment, func.count(Application.id)).group_by(
            Application.environment
        )
    ).all()
    return [{"environment": environment, "count": count} for environment, count in rows]


def _deployments_by_status() -> list[dict]:
    rows = db.session.execute(
        select(Deployment.status, func.count(Deployment.id)).group_by(Deployment.status)
    ).all()
    return [{"status": status, "count": count} for status, count in rows]
