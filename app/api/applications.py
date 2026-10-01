"""Application registry endpoints."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from extensions import db
from flask import Blueprint, jsonify, request
from models import (
    APPLICATION_ENVIRONMENTS,
    APPLICATION_STATUSES,
    HTTP_METHODS,
    Application,
    HealthCheck,
)
from security import login_required
from serializers import application_to_dict
from services import audit
from services import health as health_service
from sqlalchemy import func, or_, select

from api.helpers import (
    bool_param,
    envelope,
    int_param,
    json_error,
    load_application_or_404,
    pagination_args,
    success,
    wants_pagination,
    write_required,
)

logger = logging.getLogger("cloudpulse.api.applications")

blueprint = Blueprint("applications_api", __name__, url_prefix="/api/applications")

SORT_COLUMNS = {
    "id": Application.id,
    "name": Application.name,
    "environment": Application.environment,
    "status": Application.status,
    "created_at": Application.created_at,
    "last_checked_at": Application.last_checked_at,
    "last_health_status": Application.last_health_status,
    "uptime_percentage": Application.uptime_percentage,
}


def _body() -> dict | None:
    data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    return data if isinstance(data, dict) else None


def _validate(data: dict, application: Application | None = None) -> dict:
    """Validate a create/update payload and return a field -> value mapping."""
    errors: dict[str, str] = {}
    values: dict[str, object] = {}

    if "name" in data:
        name = str(data["name"] or "").strip()
        if not name:
            errors["name"] = "Name is required"
        elif len(name) > 100:
            errors["name"] = "Name must be 100 characters or fewer"
        else:
            values["name"] = name

    if "url" in data:
        url = str(data["url"] or "").strip()
        if not url:
            errors["url"] = "URL is required"
        else:
            url_error = health_service.validate_url(url)
            if url_error:
                errors["url"] = url_error
            else:
                values["url"] = url

    for field, allowed in (
        ("environment", APPLICATION_ENVIRONMENTS),
        ("status", APPLICATION_STATUSES),
        ("http_method", HTTP_METHODS),
    ):
        if field not in data:
            continue
        value = str(data[field] or "").strip()
        if value not in allowed:
            errors[field] = f"Must be one of: {', '.join(allowed)}"
        else:
            values[field] = value

    if "description" in data:
        description = data["description"]
        if description is not None and not isinstance(description, str):
            errors["description"] = "Description must be text"
        else:
            values["description"] = (description or "").strip() or None

    if "timeout_seconds" in data:
        try:
            timeout = int(data["timeout_seconds"])
        except (TypeError, ValueError):
            errors["timeout_seconds"] = "Must be an integer"
        else:
            if not 1 <= timeout <= 60:
                errors["timeout_seconds"] = "Must be between 1 and 60 seconds"
            else:
                values["timeout_seconds"] = timeout

    if "check_interval_seconds" in data:
        try:
            interval = int(data["check_interval_seconds"])
        except (TypeError, ValueError):
            errors["check_interval_seconds"] = "Must be an integer"
        else:
            if not 15 <= interval <= 86400:
                errors["check_interval_seconds"] = "Must be between 15 and 86400 seconds"
            else:
                values["check_interval_seconds"] = interval

    if "expected_status_codes" in data:
        spec = str(data["expected_status_codes"] or "").strip()
        if not spec:
            errors["expected_status_codes"] = "Specification is required"
        else:
            values["expected_status_codes"] = spec

    if "expected_body_keyword" in data:
        keyword = data["expected_body_keyword"]
        values["expected_body_keyword"] = (str(keyword).strip() or None) if keyword else None

    if "request_headers" in data:
        headers = data["request_headers"]
        if headers in (None, "", {}):
            values["request_headers"] = None
        elif isinstance(headers, dict):
            values["request_headers"] = json.dumps(headers)
        elif isinstance(headers, str):
            try:
                parsed = json.loads(headers)
            except ValueError:
                errors["request_headers"] = "Must be a JSON object"
            else:
                if not isinstance(parsed, dict):
                    errors["request_headers"] = "Must be a JSON object"
                else:
                    values["request_headers"] = json.dumps(parsed)
        else:
            errors["request_headers"] = "Must be a JSON object"

    if "is_paused" in data:
        values["is_paused"] = bool(data["is_paused"]) and data["is_paused"] != "false"

    name = str(values.get("name") or (application.name if application else "")).strip()
    if name:
        duplicate = db.session.scalar(
            select(Application.id).where(
                Application.name == name,
                Application.id != application.id if application else Application.id.is_not(None),
            )
        )
        if duplicate:
            errors["name"] = "An application with this name already exists"

    return {"values": values, "errors": errors}


@blueprint.route("", methods=["GET"])
@login_required
def list_applications():
    statement = select(Application)

    search = (request.args.get("search") or "").strip()
    if search:
        pattern = f"%{search}%"
        statement = statement.where(
            or_(
                Application.name.ilike(pattern),
                Application.url.ilike(pattern),
                Application.description.ilike(pattern),
            )
        )

    environment = request.args.get("environment")
    if environment:
        statement = statement.where(Application.environment == environment)

    status = request.args.get("status")
    if status:
        statement = statement.where(Application.status == status)

    health = request.args.get("health")
    if health:
        statement = statement.where(Application.last_health_status == health)

    paused = bool_param("is_paused")
    if paused is not None:
        statement = statement.where(Application.is_paused.is_(paused))

    sort = request.args.get("sort", "id")
    column = SORT_COLUMNS.get(sort, Application.id)
    order = request.args.get("order", "desc").lower()
    statement = statement.order_by(
        column.asc() if order == "asc" else column.desc()
    )

    total = db.session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    page, per_page, offset = pagination_args()
    applications = list(db.session.scalars(statement.offset(offset).limit(per_page)))

    payload = [application_to_dict(application) for application in applications]

    if wants_pagination():
        return jsonify(envelope(payload, total, page, per_page))

    return jsonify(payload)


@blueprint.route("/metadata", methods=["GET"])
@login_required
def metadata():
    return jsonify(
        {
            "environments": list(APPLICATION_ENVIRONMENTS),
            "statuses": list(APPLICATION_STATUSES),
            "http_methods": list(HTTP_METHODS),
            "sort_columns": sorted(SORT_COLUMNS),
        }
    )


@blueprint.route("", methods=["POST"])
@write_required
def create_application():
    data = _body()
    if not data:
        return json_error("Request body is required", 400)

    result = _validate(data)
    if result["errors"]:
        return json_error("Validation failed", 422, fields=result["errors"])

    values = result["values"]
    for field in ("name", "url", "environment"):
        if field not in values:
            return json_error(f"'{field}' is required", 422)

    application = Application(**values)
    db.session.add(application)
    db.session.commit()

    audit.record(
        "application.created",
        entity_type="application",
        entity_id=application.id,
        summary=f"Created application '{application.name}'",
        detail=values,
    )

    return success(
        "Application created successfully",
        application=application_to_dict(application),
    ), 201


@blueprint.route("/<int:application_id>", methods=["GET"])
@login_required
def get_application(application_id: int):
    application = load_application_or_404(application_id)
    if not application:
        return json_error("Application not found", 404)

    recent = list(
        db.session.scalars(
            select(HealthCheck)
            .where(HealthCheck.application_id == application.id)
            .order_by(HealthCheck.checked_at.desc())
            .limit(10)
        )
    )

    payload = application_to_dict(application)
    payload["recent_checks"] = [
        {
            "status": check.status,
            "http_status": check.http_status,
            "response_time": check.response_time,
            "checked_at": check.checked_at.isoformat() if check.checked_at else None,
        }
        for check in recent
    ]

    return jsonify(payload)


@blueprint.route("/<int:application_id>", methods=["PUT", "PATCH"])
@write_required
def update_application(application_id: int):
    application = load_application_or_404(application_id)
    if not application:
        return json_error("Application not found", 404)

    data = _body()
    if not data:
        return json_error("Request body is required", 400)

    result = _validate(data, application)
    if result["errors"]:
        return json_error("Validation failed", 422, fields=result["errors"])

    changed = {}
    for field, value in result["values"].items():
        if getattr(application, field) != value:
            changed[field] = {"from": getattr(application, field), "to": value}
            setattr(application, field, value)

    if not changed:
        return success("No changes were supplied", application=application_to_dict(application))

    db.session.commit()
    audit.record(
        "application.updated",
        entity_type="application",
        entity_id=application.id,
        summary=f"Updated application '{application.name}'",
        detail=changed,
    )

    return success(
        "Application updated successfully",
        application=application_to_dict(application),
    )


@blueprint.route("/<int:application_id>", methods=["DELETE"])
@write_required
def delete_application(application_id: int):
    application = load_application_or_404(application_id)
    if not application:
        return json_error("Application not found", 404)

    name = application.name

    try:
        db.session.delete(application)
        db.session.commit()
    except Exception as error:  # pragma: no cover - defensive
        db.session.rollback()
        logger.exception("Failed to delete application | ID: %s", application_id)
        return json_error("Failed to delete application", 500, details=str(error))

    audit.record(
        "application.deleted",
        entity_type="application",
        entity_id=application_id,
        summary=f"Deleted application '{name}'",
    )

    return success("Application deleted successfully")


@blueprint.route("/<int:application_id>/check", methods=["POST"])
@write_required
def run_check(application_id: int):
    application = load_application_or_404(application_id)
    if not application:
        return json_error("Application not found", 404)

    result = health_service.check_application_health(application)
    return jsonify(result)


@blueprint.route("/<int:application_id>/health", methods=["GET"])
@login_required
@login_required
def application_health(application_id: int):
    """Legacy endpoint kept for backwards compatibility.

    Runs a live health check and reconciles incidents, preserving the original
    CloudPulse response shape (``incident_created`` / ``incident_id``).
    """
    application = load_application_or_404(application_id)
    if not application:
        return json_error("Application not found", 404)

    before = {incident.id for incident in application.incidents}

    result = health_service.check_application_health(application)

    application = load_application_or_404(application_id)
    opened = [
        incident
        for incident in application.incidents
        if incident.id not in before and incident.source == "monitoring"
    ]

    result["incident_created"] = bool(opened)
    if opened:
        result["incident_id"] = opened[0].id

    return jsonify(result)


@blueprint.route("/<int:application_id>/health-history", methods=["GET"])
@login_required
def application_health_history(application_id: int):
    application = load_application_or_404(application_id)
    if not application:
        return json_error("Application not found", 404)

    # ``pagination_args`` already falls back to ?limit= when ?per_page= is absent
    hours = int_param("hours", None, minimum=1, maximum=24 * 90)
    page, per_page, offset = pagination_args()

    payload = health_service.health_history(
        application,
        limit=per_page,
        hours=hours,
        offset=offset,
    )

    if wants_pagination():
        total = (
            db.session.scalar(
                select(func.count(HealthCheck.id)).where(
                    HealthCheck.application_id == application.id
                )
            )
            or 0
        )
        return jsonify(envelope(payload["history"], total, page, per_page, summary=payload["summary"],
                                application_id=payload["application_id"],
                                application_name=payload["application_name"],
                                latest_status=payload["latest_status"]))

    return jsonify(payload), 200


@blueprint.route("/<int:application_id>/uptime", methods=["GET"])
@login_required
def application_uptime(application_id: int):
    application = load_application_or_404(application_id)
    if not application:
        return json_error("Application not found", 404)

    days = int_param("days", 7, minimum=1, maximum=365) or 7
    return jsonify(health_service.uptime_report(application, days))


@blueprint.route("/<int:application_id>/pause", methods=["POST"])
@write_required
def pause_application(application_id: int):
    return _set_paused(application_id, True)


@blueprint.route("/<int:application_id>/resume", methods=["POST"])
@write_required
def resume_application(application_id: int):
    return _set_paused(application_id, False)


def _set_paused(application_id: int, paused: bool):
    application = load_application_or_404(application_id)
    if not application:
        return json_error("Application not found", 404)

    if application.is_paused == paused:
        return success(
            f"Application is already {'paused' if paused else 'active'}",
            application=application_to_dict(application),
        )

    application.is_paused = paused
    application.last_checked_at = datetime.now(UTC) if not paused else application.last_checked_at
    db.session.commit()

    audit.record(
        "application.paused" if paused else "application.resumed",
        entity_type="application",
        entity_id=application.id,
        summary=f"{'Paused' if paused else 'Resumed'} monitoring for '{application.name}'",
    )

    return success(
        f"Application {'paused' if paused else 'resumed'} successfully",
        application=application_to_dict(application),
    )
