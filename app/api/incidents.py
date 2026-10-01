"""Incident management endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from extensions import db
from flask import Blueprint, jsonify, request
from models import (
    INCIDENT_SEVERITIES,
    INCIDENT_STATUSES,
    Application,
    AuditLog,
    Incident,
    severity_rank,
)
from security import login_required
from serializers import audit_log_to_dict, incident_to_dict
from services import audit, notifications
from services import incidents as incident_service
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

logger = logging.getLogger("cloudpulse.api.incidents")

blueprint = Blueprint("incidents_api", __name__, url_prefix="/api/incidents")

SORT_COLUMNS = {
    "id": Incident.id,
    "title": Incident.title,
    "severity": Incident.severity,
    "status": Incident.status,
    "detected_at": Incident.detected_at,
    "created_at": Incident.created_at,
    "resolved_at": Incident.resolved_at,
}


def _sla() -> dict:
    return incident_service.sla_config()


def _body() -> dict | None:
    data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    return data if isinstance(data, dict) else None


def _validate(data: dict, incident: Incident | None = None) -> dict:
    errors: dict[str, str] = {}
    values: dict[str, object] = {}

    if "application_id" in data:
        try:
            application_id = int(data["application_id"])
        except (TypeError, ValueError):
            errors["application_id"] = "Must be a valid application id"
        else:
            if not db.session.get(Application, application_id):
                errors["application_id"] = "Application not found"
            else:
                values["application_id"] = application_id

    if "title" in data:
        title = str(data["title"] or "").strip()
        if not title:
            errors["title"] = "Title is required"
        elif len(title) > 200:
            errors["title"] = "Title must be 200 characters or fewer"
        else:
            values["title"] = title

    if "description" in data:
        description = data["description"]
        if description is not None and not isinstance(description, str):
            errors["description"] = "Description must be text"
        else:
            values["description"] = (description or "").strip() or None

    if "root_cause" in data:
        root_cause = data["root_cause"]
        values["root_cause"] = (str(root_cause).strip() or None) if root_cause else None

    if "assignee" in data:
        assignee = data["assignee"]
        values["assignee"] = (str(assignee).strip() or None) if assignee else None

    if "severity" in data:
        severity = str(data["severity"] or "").strip()
        if severity not in INCIDENT_SEVERITIES:
            errors["severity"] = f"Must be one of: {', '.join(INCIDENT_SEVERITIES)}"
        else:
            values["severity"] = severity

    if "status" in data:
        status = str(data["status"] or "").strip()
        if status not in INCIDENT_STATUSES:
            errors["status"] = f"Must be one of: {', '.join(INCIDENT_STATUSES)}"
        else:
            values["status"] = status

    return {"values": values, "errors": errors}


@blueprint.route("", methods=["GET"])
@login_required
def list_incidents():
    statement = select(Incident).join(Application, Incident.application_id == Application.id)

    search = (request.args.get("search") or "").strip()
    if search:
        pattern = f"%{search}%"
        statement = statement.where(
            or_(
                Incident.title.ilike(pattern),
                Incident.description.ilike(pattern),
                Application.name.ilike(pattern),
            )
        )

    status = request.args.get("status")
    if status:
        statement = statement.where(Incident.status == status)

    open_only = request.args.get("open") in {"1", "true", "True"}
    if open_only:
        statement = statement.where(Incident.status != "Resolved")

    severity = request.args.get("severity")
    if severity:
        statement = statement.where(Incident.severity == severity)

    environment = request.args.get("environment")
    if environment:
        statement = statement.where(Application.environment == environment)

    application_id = int_param("application_id")
    if application_id:
        statement = statement.where(Incident.application_id == application_id)

    source = request.args.get("source")
    if source:
        statement = statement.where(Incident.source == source)

    breached_only = request.args.get("breached") in {"1", "true", "True"}
    if breached_only:
        statement = statement.where(Incident.status != "Resolved")

    sort = request.args.get("sort", "detected_at")
    column = SORT_COLUMNS.get(sort, Incident.detected_at)
    order = request.args.get("order", "desc").lower()
    statement = statement.order_by(
        column.asc() if order == "asc" else column.desc()
    )

    total = db.session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    page, per_page, offset = pagination_args()
    rows = list(db.session.scalars(statement.offset(offset).limit(per_page)))

    sla = _sla()
    payload = [incident_to_dict(row, sla) for row in rows]

    if breached_only:
        payload = [item for item in payload if item.get("sla_breached")]

    if wants_pagination():
        return jsonify(
            envelope(
                payload,
                total,
                page,
                per_page,
                summary={
                    "open": incident_service.count_open(),
                    "severity": incident_service.severity_breakdown(),
                },
            )
        )

    return jsonify(payload)


@blueprint.route("/metadata", methods=["GET"])
@login_required
def metadata():
    return jsonify(
        {
            "severities": list(INCIDENT_SEVERITIES),
            "statuses": list(INCIDENT_STATUSES),
            "sort_columns": sorted(SORT_COLUMNS),
            "sla_minutes": _sla(),
        }
    )


@blueprint.route("", methods=["POST"])
@write_required
def create_incident():
    data = _body()
    if not data:
        return json_error("Request body is required", 400)

    if "application_id" not in data or "title" not in data:
        return json_error("'application_id' and 'title' are required", 422)

    result = _validate(data)
    if result["errors"]:
        return json_error("Validation failed", 422, fields=result["errors"])

    values = result["values"]
    incident = Incident(
        source=str(data.get("source", "manual")),
        detected_at=datetime.now(UTC),
        **values,
    )

    if incident.status == "Resolved":
        incident.resolved_at = datetime.now(UTC)

    db.session.add(incident)
    db.session.commit()

    audit.record(
        "incident.created",
        entity_type="incident",
        entity_id=incident.id,
        summary=f"Created incident '{incident.title}'",
        detail=values,
    )
    notifications.notify_incident_opened(incident)

    return success(
        "Incident created successfully",
        incident=incident_to_dict(incident, _sla()),
    ), 201


@blueprint.route("/stats", methods=["GET"])
@login_required
def incident_stats():
    days = int_param("days", 30, minimum=1, maximum=365) or 30
    return jsonify(
        {
            "reliability": incident_service.statistics(days),
            "severity_breakdown": incident_service.severity_breakdown(),
            "status_breakdown": incident_service.status_breakdown(),
            "open_incidents": [
                {
                    "id": incident.id,
                    "title": incident.title,
                    "severity": incident.severity,
                    "application": (
                        incident.application.name if incident.application else None
                    ),
                }
                for incident in incident_service.breached_incidents()
            ],
        }
    )


@blueprint.route("/<int:incident_id>", methods=["GET"])
@login_required
def get_incident(incident_id: int):
    incident = db.session.get(Incident, incident_id)
    if not incident:
        return json_error("Incident not found", 404)

    payload = incident_to_dict(incident, _sla())

    timeline = list(
        db.session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "incident", AuditLog.entity_id == incident.id)
            .order_by(AuditLog.created_at.asc())
        )
    )
    payload["timeline"] = [audit_log_to_dict(entry) for entry in timeline]

    return jsonify(payload)


@blueprint.route("/<int:incident_id>", methods=["PUT", "PATCH"])
@write_required
def update_incident(incident_id: int):
    incident = db.session.get(Incident, incident_id)
    if not incident:
        return json_error("Incident not found", 404)

    data = _body()
    if not data:
        return json_error("Request body is required", 400)

    result = _validate(data, incident)
    if result["errors"]:
        return json_error("Validation failed", 422, fields=result["errors"])

    changed = {}
    previous_status = incident.status

    for field, value in result["values"].items():
        if getattr(incident, field) != value:
            changed[field] = {"from": getattr(incident, field), "to": value}
            setattr(incident, field, value)

    if "status" in changed:
        if incident.status == "Resolved":
            incident.resolved_at = incident.resolved_at or datetime.now(UTC)
        else:
            incident.resolved_at = None
            if incident.acknowledged_at is None:
                incident.acknowledged_at = datetime.now(UTC)

    if not changed:
        return success("No changes were supplied", incident=incident_to_dict(incident, _sla()))

    db.session.commit()
    audit.record(
        "incident.updated",
        entity_type="incident",
        entity_id=incident.id,
        summary=f"Updated incident '{incident.title}'",
        detail=changed,
    )

    if previous_status != "Resolved" and incident.status == "Resolved":
        notifications.notify_incident_resolved(incident)

    return success(
        "Incident updated successfully",
        incident=incident_to_dict(incident, _sla()),
    )


@blueprint.route("/<int:incident_id>/resolve", methods=["PUT", "POST"])
@write_required
def resolve_incident(incident_id: int):
    incident = db.session.get(Incident, incident_id)
    if not incident:
        return json_error("Incident not found", 404)

    data = _body() or {}
    already_resolved = incident.status == "Resolved"

    incident_service.resolve(
        incident,
        note=str(data.get("root_cause") or data.get("note") or "").strip() or None,
    )
    db.session.commit()

    audit.record(
        "incident.resolved",
        entity_type="incident",
        entity_id=incident.id,
        summary=f"Resolved incident '{incident.title}'",
        detail={"already_resolved": already_resolved},
    )
    notifications.notify_incident_resolved(incident)

    return success(
        "Incident resolved successfully",
        incident=incident_to_dict(incident, _sla()),
    )


@blueprint.route("/<int:incident_id>/acknowledge", methods=["POST"])
@write_required
def acknowledge_incident(incident_id: int):
    incident = db.session.get(Incident, incident_id)
    if not incident:
        return json_error("Incident not found", 404)

    incident_service.acknowledge(incident)
    db.session.commit()

    audit.record(
        "incident.acknowledged",
        entity_type="incident",
        entity_id=incident.id,
        summary=f"Acknowledged incident '{incident.title}'",
    )

    return success(
        "Incident acknowledged",
        incident=incident_to_dict(incident, _sla()),
    )


@blueprint.route("/<int:incident_id>/escalate", methods=["POST"])
@write_required
def escalate_incident(incident_id: int):
    incident = db.session.get(Incident, incident_id)
    if not incident:
        return json_error("Incident not found", 404)

    previous = incident.severity
    escalated = incident_service.escalate(incident)

    if escalated is None:
        return json_error("Incident is already at Critical severity", 409)

    db.session.commit()
    audit.record(
        "incident.escalated",
        entity_type="incident",
        entity_id=incident.id,
        summary=f"Escalated incident '{incident.title}' from {previous} to {incident.severity}",
    )

    return success(
        "Incident escalated",
        incident=incident_to_dict(incident, _sla()),
    )


@blueprint.route("/bulk", methods=["POST"])
@write_required
def bulk_action():
    data = _body() or {}
    action = str(data.get("action") or "").strip().lower()
    ids = data.get("ids") or []

    if not isinstance(ids, list) or not ids:
        return json_error("'ids' must be a non-empty list", 422)

    try:
        ids = [int(value) for value in ids]
    except (TypeError, ValueError):
        return json_error("'ids' must contain integers", 422)

    rows = list(db.session.scalars(select(Incident).where(Incident.id.in_(ids))))
    found = {row.id for row in rows}

    if action == "resolve":
        now = datetime.now(UTC)
        for incident in rows:
            if incident.status != "Resolved":
                incident.status = "Resolved"
                incident.resolved_at = now
        db.session.commit()
        audit.record(
            "incident.bulk_resolved",
            entity_type="incident",
            summary=f"Resolved {len(rows)} incidents",
            detail={"ids": sorted(found)},
        )
        return success(
            f"Resolved {len(rows)} incidents",
            updated=len(rows),
            not_found=sorted(set(ids) - found),
        )

    if action == "delete":
        for incident in rows:
            db.session.delete(incident)
        db.session.commit()
        audit.record(
            "incident.bulk_deleted",
            entity_type="incident",
            summary=f"Deleted {len(rows)} incidents",
            detail={"ids": sorted(found)},
        )
        return success(
            f"Deleted {len(rows)} incidents",
            deleted=len(rows),
            not_found=sorted(set(ids) - found),
        )

    return json_error(
        f"Unsupported bulk action '{action}'",
        400,
        supported=["resolve", "delete"],
    )


@blueprint.route("/<int:incident_id>", methods=["DELETE"])
@write_required
def delete_incident(incident_id: int):
    incident = db.session.get(Incident, incident_id)
    if not incident:
        return json_error("Incident not found", 404)

    title = incident.title
    db.session.delete(incident)
    db.session.commit()

    audit.record(
        "incident.deleted",
        entity_type="incident",
        entity_id=incident_id,
        summary=f"Deleted incident '{title}'",
    )

    return success("Incident deleted successfully")


def sort_incidents_by_severity(items: list[Incident]) -> list[Incident]:
    return sorted(items, key=lambda item: severity_rank(item.severity), reverse=True)
