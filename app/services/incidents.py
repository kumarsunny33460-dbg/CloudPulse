"""Incident lifecycle rules: creation, de-duplication, auto resolution, SLA."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from extensions import db
from flask import current_app
from models import (
    Application,
    Incident,
    Notification,
    severity_rank,
)
from sqlalchemy import func, select

from services import audit, notifications

logger = logging.getLogger("cloudpulse.incidents")

DEFAULT_SLA_MINUTES = {
    "Critical": 30,
    "High": 120,
    "Medium": 480,
    "Low": 1440,
}


def sla_config() -> dict:
    configured = None
    try:
        configured = current_app.config.get("SLA_MINUTES")
    except RuntimeError:
        configured = None
    return configured or DEFAULT_SLA_MINUTES


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def open_incident_for(application_id: int) -> Incident | None:
    return db.session.scalar(
        select(Incident)
        .where(
            Incident.application_id == application_id,
            Incident.status != "Resolved",
        )
        .order_by(Incident.detected_at.desc())
        .limit(1)
    )


def count_open() -> int:
    return (
        db.session.scalar(
            select(func.count(Incident.id)).where(Incident.status != "Resolved")
        )
        or 0
    )


def create_incident_if_needed(application: Application, health_result: dict) -> Incident | None:
    """Open a High severity incident when monitoring detects an outage.

    De-duplicates against any incident that is still open for the application so
    a flapping service never produces a flood of tickets. Flushes instead of
    committing: the caller owns the transaction.
    """
    if health_result.get("health") != "DOWN":
        return None

    existing = open_incident_for(application.id)
    if existing is not None:
        logger.debug(
            "Existing open incident found | Application: %s | Incident: %s",
            application.name,
            existing.id,
        )
        return existing

    incident = Incident(
        application_id=application.id,
        title=f"{application.name} is Down",
        description=(
            f"CloudPulse detected that {application.name} is currently unreachable. "
            f"Details: {health_result.get('message', 'health check failed')}"
        ),
        severity="High",
        status="Open",
        source="monitoring",
        detected_at=_as_aware(datetime.fromisoformat(health_result["checked_at"])),
    )

    db.session.add(incident)
    db.session.flush()

    logger.warning(
        "New incident created | Application: %s | Incident: %s | Severity: High",
        application.name,
        incident.id,
    )

    audit.record(
        "incident.auto_opened",
        entity_type="incident",
        entity_id=incident.id,
        summary=f"Monitoring opened incident '{incident.title}'",
        detail={"health": health_result},
    )

    notifications.notify_incident_opened(incident)
    return incident


def auto_resolve_for_application(
    application: Application,
    *,
    note: str | None = None,
) -> Incident | None:
    """Resolve monitoring-created incidents once the application recovers."""
    candidates = list(
        db.session.scalars(
            select(Incident).where(
                Incident.application_id == application.id,
                Incident.status != "Resolved",
                Incident.source == "monitoring",
            )
        )
    )

    if not candidates:
        return None

    resolved_at = _now()
    for incident in candidates:
        incident.status = "Resolved"
        incident.resolved_at = resolved_at
        if note and not incident.root_cause:
            incident.root_cause = note

    db.session.flush()

    primary = candidates[0]
    logger.info(
        "Auto resolved %s incident(s) | Application: %s | Incident: %s",
        len(candidates),
        application.name,
        primary.id,
    )

    audit.record(
        "incident.auto_resolved",
        entity_type="incident",
        entity_id=primary.id,
        summary=f"Monitoring resolved incident '{primary.title}'",
        detail={"note": note},
    )

    notifications.notify_incident_resolved(primary)
    return primary


def resolve(incident: Incident, *, note: str | None = None) -> Incident:
    incident.status = "Resolved"
    incident.resolved_at = _now()
    if note:
        incident.root_cause = note
    return incident


def acknowledge(incident: Incident) -> Incident:
    if incident.status == "Open":
        incident.status = "Investigating"
    if incident.acknowledged_at is None:
        incident.acknowledged_at = _now()
    return incident


def escalate(incident: Incident) -> Incident | None:
    """Raise the severity of an incident one level."""
    order = ["Low", "Medium", "High", "Critical"]
    current = order.index(incident.severity) if incident.severity in order else 0
    if current >= len(order) - 1:
        return None
    incident.severity = order[current + 1]
    return incident


def sla_state(incident: Incident) -> dict:
    sla = sla_config()
    target = sla.get(incident.severity)
    detected = _as_aware(incident.detected_at or incident.created_at)
    if not target or not detected:
        return {}

    anchor = _as_aware(incident.resolved_at) or _now()
    elapsed = (anchor - detected).total_seconds() / 60

    return {
        "sla_minutes": target,
        "sla_elapsed_minutes": round(elapsed, 2),
        "sla_remaining_minutes": round(target - elapsed, 2),
        "sla_breached": bool(elapsed > target and incident.status != "Resolved"),
    }


def breached_incidents() -> list[Incident]:
    """Open incidents that have exceeded their SLA target."""
    now = _now()
    sla = sla_config()
    breached = []

    for incident in db.session.scalars(
        select(Incident).where(Incident.status != "Resolved")
    ):
        target = sla.get(incident.severity)
        detected = _as_aware(incident.detected_at or incident.created_at)
        if target and detected and (now - detected).total_seconds() / 60 > target:
            breached.append(incident)

    return breached


def statistics(days: int = 30) -> dict:
    """Reliability KPIs used by the overview dashboard."""
    days = max(1, int(days))
    since = _now() - timedelta(days=days)
    sla = sla_config()

    resolved = list(
        db.session.scalars(
            select(Incident).where(
                Incident.status == "Resolved",
                Incident.resolved_at.is_not(None),
                Incident.resolved_at >= since,
            )
        )
    )

    durations = []
    for incident in resolved:
        detected = _as_aware(incident.detected_at or incident.created_at)
        finished = _as_aware(incident.resolved_at)
        if detected and finished:
            durations.append((finished - detected).total_seconds() / 60)

    sla_breaches = 0
    for incident in resolved:
        detected = _as_aware(incident.detected_at or incident.created_at)
        finished = _as_aware(incident.resolved_at)
        target = sla.get(incident.severity)
        if (
            detected
            and finished
            and target
            and (finished - detected).total_seconds() / 60 > target
        ):
            sla_breaches += 1

    for incident in breached_incidents():
        if incident not in resolved:
            sla_breaches += 1

    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(int(round((percentile / 100) * (len(ordered) - 1))), len(ordered) - 1)
        return round(ordered[index], 2)

    return {
        "window_days": days,
        "total_incidents": (
            db.session.scalar(select(func.count(Incident.id))) or 0
        ),
        "open_incidents": count_open(),
        "resolved_incidents": len(resolved),
        "mttr_minutes": (
            round(sum(durations) / len(durations), 2) if durations else None
        ),
        "p50_resolution_minutes": _percentile(durations, 50),
        "p90_resolution_minutes": _percentile(durations, 90),
        "fastest_resolution_minutes": _percentile(durations, 0),
        "sla_breaches": sla_breaches,
        "sla_compliance_percentage": (
            round(((len(resolved) - sla_breaches) / len(resolved)) * 100, 2)
            if resolved
            else 100.0
        ),
    }


def severity_breakdown() -> list[dict]:
    rows = db.session.execute(
        select(Incident.severity, func.count(Incident.id))
        .group_by(Incident.severity)
    ).all()

    counts = dict.fromkeys(("Critical", "High", "Medium", "Low"), 0)
    for severity, total in rows:
        counts[severity] = total

    open_counts = dict(
        db.session.execute(
            select(Incident.severity, func.count(Incident.id))
            .where(Incident.status != "Resolved")
            .group_by(Incident.severity)
        ).all()
    )

    return [
        {
            "severity": severity,
            "count": count,
            "open": open_counts.get(severity, 0),
            "rank": severity_rank(severity),
        }
        for severity, count in sorted(
            counts.items(), key=lambda item: severity_rank(item[0]), reverse=True
        )
    ]


def status_breakdown() -> list[dict]:
    rows = db.session.execute(
        select(Incident.status, func.count(Incident.id)).group_by(Incident.status)
    ).all()
    return [{"status": status, "count": count} for status, count in rows]


def notification_failures(limit: int = 20) -> int:
    return (
        db.session.scalar(
            select(func.count(Notification.id)).where(Notification.status == "failed")
        )
        or 0
    )
