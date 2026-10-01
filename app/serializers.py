"""Serializers converting model instances into JSON friendly payloads.

Every serializer stays backwards compatible with the original CloudPulse API
contract: the original keys are always present, new capabilities are added
alongside them so existing clients keep working.
"""

from __future__ import annotations

from datetime import UTC, datetime

from models import (
    Application,
    AuditLog,
    Deployment,
    HealthCheck,
    Incident,
    Notification,
    User,
    severity_rank,
)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "created_at": isoformat(user.created_at),
    }


def application_to_dict(application: Application) -> dict:
    open_incidents = [
        incident
        for incident in application.incidents
        if incident.status != "Resolved"
    ]
    last_deployment = (
        max(application.deployments, key=lambda item: item.deployed_at)
        if application.deployments
        else None
    )

    return {
        "id": application.id,
        "name": application.name,
        "url": application.url,
        "environment": application.environment,
        "status": application.status,
        "description": application.description,
        "created_at": isoformat(application.created_at),
        "last_checked_at": isoformat(application.last_checked_at),
        "last_health_status": application.last_health_status,
        "last_response_time": application.last_response_time,
        "last_http_status": application.last_http_status,
        "uptime_percentage": application.uptime_percentage,
        "consecutive_failures": application.consecutive_failures,
        "is_paused": application.is_paused,
        "open_incident_count": len(open_incidents),
        "open_incident_ids": [incident.id for incident in open_incidents],
        "last_deployment": (
            {
                "id": last_deployment.id,
                "version": last_deployment.version,
                "status": last_deployment.status,
                "deployed_at": isoformat(last_deployment.deployed_at),
            }
            if last_deployment
            else None
        ),
        "monitoring": application.monitoring_summary(),
    }


def incident_to_dict(incident: Incident, sla_minutes: dict | None = None) -> dict:
    detected_at = incident.detected_at or incident.created_at
    resolved_at = incident.resolved_at

    duration_minutes = None
    if detected_at and resolved_at:
        start = (
            detected_at
            if detected_at.tzinfo
            else detected_at.replace(tzinfo=UTC)
        )
        end = resolved_at if resolved_at.tzinfo else resolved_at.replace(tzinfo=UTC)
        duration_minutes = round((end - start).total_seconds() / 60, 2)

    payload = {
        "id": incident.id,
        "application_id": incident.application_id,
        "application_name": (
            incident.application.name if incident.application else None
        ),
        "application_url": (
            incident.application.url if incident.application else None
        ),
        "title": incident.title,
        "description": incident.description,
        "root_cause": incident.root_cause,
        "assignee": incident.assignee,
        "severity": incident.severity,
        "severity_rank": severity_rank(incident.severity),
        "status": incident.status,
        "source": incident.source,
        "created_at": isoformat(incident.created_at),
        "detected_at": isoformat(detected_at),
        "acknowledged_at": isoformat(incident.acknowledged_at),
        "resolved_at": isoformat(resolved_at),
        "duration_minutes": duration_minutes,
    }

    if sla_minutes:
        target = sla_minutes.get(incident.severity)
        if target and detected_at:
            start = (
                detected_at
                if detected_at.tzinfo
                else detected_at.replace(tzinfo=UTC)
            )
            anchor = resolved_at or datetime.now(UTC)
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=UTC)
            elapsed = (anchor - start).total_seconds() / 60
            payload["sla_minutes"] = target
            payload["sla_elapsed_minutes"] = round(elapsed, 2)
            payload["sla_breached"] = bool(elapsed > target)
            payload["sla_remaining_minutes"] = round(target - elapsed, 2)

    return payload


def deployment_to_dict(deployment: Deployment) -> dict:
    return {
        "id": deployment.id,
        "application_id": deployment.application_id,
        "application_name": (
            deployment.application.name if deployment.application else None
        ),
        "version": deployment.version,
        "environment": deployment.environment,
        "status": deployment.status,
        "commit_hash": deployment.commit_hash,
        "changelog": deployment.changelog,
        "duration_seconds": deployment.duration_seconds,
        "deployed_by": deployment.deployed_by,
        "created_at": isoformat(deployment.created_at),
        "deployed_at": isoformat(deployment.deployed_at),
        "rolled_back_at": isoformat(deployment.rolled_back_at),
    }


def health_check_to_dict(check: HealthCheck) -> dict:
    return {
        "id": check.id,
        "application_id": check.application_id,
        "status": check.status,
        "http_status": check.http_status,
        "response_time": check.response_time,
        "checked_at": isoformat(check.checked_at),
        "error_message": check.error_message,
        "triggered_incident_id": check.triggered_incident_id,
    }


def audit_log_to_dict(entry: AuditLog) -> dict:
    return {
        "id": entry.id,
        "actor_id": entry.actor_id,
        "actor_name": entry.actor_name,
        "action": entry.action,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "summary": entry.summary,
        "detail": entry.detail,
        "ip_address": entry.ip_address,
        "created_at": isoformat(entry.created_at),
    }


def notification_to_dict(notification: Notification) -> dict:
    return {
        "id": notification.id,
        "incident_id": notification.incident_id,
        "application_id": notification.application_id,
        "channel": notification.channel,
        "target": notification.target,
        "event": notification.event,
        "status": notification.status,
        "attempts": notification.attempts,
        "last_error": notification.last_error,
        "sent_at": isoformat(notification.sent_at),
        "created_at": isoformat(notification.created_at),
    }
