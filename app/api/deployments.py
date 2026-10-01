"""Deployment tracking endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from extensions import db
from flask import Blueprint, jsonify, request, session
from models import (
    APPLICATION_ENVIRONMENTS,
    DEPLOYMENT_STATUSES,
    Application,
    Deployment,
    Incident,
)
from security import login_required
from serializers import deployment_to_dict
from services import audit, notifications
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

logger = logging.getLogger("cloudpulse.api.deployments")

blueprint = Blueprint("deployments_api", __name__, url_prefix="/api/deployments")

SORT_COLUMNS = {
    "id": Deployment.id,
    "version": Deployment.version,
    "environment": Deployment.environment,
    "status": Deployment.status,
    "deployed_at": Deployment.deployed_at,
    "created_at": Deployment.created_at,
}


def _body() -> dict | None:
    data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    return data if isinstance(data, dict) else None


def _validate(data: dict) -> dict:
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

    if "version" in data:
        version = str(data["version"] or "").strip()
        if not version:
            errors["version"] = "Version is required"
        elif len(version) > 100:
            errors["version"] = "Version must be 100 characters or fewer"
        else:
            values["version"] = version

    if "environment" in data:
        environment = str(data["environment"] or "").strip()
        if environment not in APPLICATION_ENVIRONMENTS:
            errors["environment"] = f"Must be one of: {', '.join(APPLICATION_ENVIRONMENTS)}"
        else:
            values["environment"] = environment

    if "status" in data:
        status = str(data["status"] or "").strip()
        if status not in DEPLOYMENT_STATUSES:
            errors["status"] = f"Must be one of: {', '.join(DEPLOYMENT_STATUSES)}"
        else:
            values["status"] = status

    if "commit_hash" in data:
        commit_hash = str(data["commit_hash"] or "").strip()
        if commit_hash and len(commit_hash) > 64:
            errors["commit_hash"] = "Commit hash must be 64 characters or fewer"
        else:
            values["commit_hash"] = commit_hash or None

    if "changelog" in data:
        changelog = data["changelog"]
        values["changelog"] = (str(changelog).strip() or None) if changelog else None

    if "duration_seconds" in data:
        duration = data["duration_seconds"]
        if duration in (None, ""):
            values["duration_seconds"] = None
        else:
            try:
                duration_value = int(duration)
            except (TypeError, ValueError):
                errors["duration_seconds"] = "Must be an integer"
            else:
                if duration_value < 0:
                    errors["duration_seconds"] = "Must not be negative"
                else:
                    values["duration_seconds"] = duration_value

    if "deployed_by" in data:
        deployed_by = str(data["deployed_by"] or "").strip()
        values["deployed_by"] = deployed_by or session.get("username")

    return {"values": values, "errors": errors}


@blueprint.route("", methods=["GET"])
@login_required
def list_deployments():
    statement = select(Deployment).join(
        Application, Deployment.application_id == Application.id
    )

    search = (request.args.get("search") or "").strip()
    if search:
        pattern = f"%{search}%"
        statement = statement.where(
            or_(
                Deployment.version.ilike(pattern),
                Deployment.changelog.ilike(pattern),
                Deployment.commit_hash.ilike(pattern),
                Application.name.ilike(pattern),
            )
        )

    for field, column in (
        ("environment", Deployment.environment),
        ("status", Deployment.status),
        ("application_id", Deployment.application_id),
    ):
        value = request.args.get(field)
        if value:
            statement = statement.where(column == value)

    since = request.args.get("since")
    if since:
        try:
            statement = statement.where(
                Deployment.deployed_at
                >= datetime.fromisoformat(since.replace("Z", "+00:00"))
            )
        except ValueError:
            return json_error("'since' must be an ISO-8601 timestamp", 400)

    sort = request.args.get("sort", "deployed_at")
    column = SORT_COLUMNS.get(sort, Deployment.deployed_at)
    order = request.args.get("order", "desc").lower()
    statement = statement.order_by(
        column.asc() if order == "asc" else column.desc()
    )

    total = db.session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    page, per_page, offset = pagination_args()
    rows = list(db.session.scalars(statement.offset(offset).limit(per_page)))

    payload = [deployment_to_dict(row) for row in rows]

    if wants_pagination():
        return jsonify(
            envelope(
                payload,
                total,
                page,
                per_page,
                summary=summary(),
            )
        )

    return jsonify(payload)


@blueprint.route("/metadata", methods=["GET"])
@login_required
def metadata():
    return jsonify(
        {
            "statuses": list(DEPLOYMENT_STATUSES),
            "environments": list(APPLICATION_ENVIRONMENTS),
            "sort_columns": sorted(SORT_COLUMNS),
        }
    )


def summary() -> dict:
    rows = db.session.execute(
        select(Deployment.status, func.count(Deployment.id)).group_by(Deployment.status)
    ).all()
    counts = dict.fromkeys(DEPLOYMENT_STATUSES, 0)
    for status, total in rows:
        counts[status] = total

    last_30_days = (
        db.session.scalar(
            select(func.count(Deployment.id)).where(
                Deployment.deployed_at
                >= datetime.now(UTC) - timedelta(days=30)
            )
        )
        or 0
    )

    return {
        "total": sum(counts.values()),
        "by_status": counts,
        "last_30_days": last_30_days,
    }


@blueprint.route("", methods=["POST"])
@write_required
def create_deployment():
    data = _body()
    if not data:
        return json_error("Request body is required", 400)

    for field in ("application_id", "version", "environment"):
        if field not in data:
            return json_error(f"'{field}' is required", 422)

    result = _validate(data)
    if result["errors"]:
        return json_error("Validation failed", 422, fields=result["errors"])

    values = dict(result["values"])
    values.setdefault("deployed_by", session.get("username"))

    deployment = Deployment(deployed_at=datetime.now(UTC), **values)
    db.session.add(deployment)
    db.session.commit()

    audit.record(
        "deployment.created",
        entity_type="deployment",
        entity_id=deployment.id,
        summary=(
            f"Recorded deployment {deployment.version} to "
            f"{deployment.environment} ({deployment.status})"
        ),
        detail=values,
    )
    notifications.notify_deployment(deployment)

    return success(
        "Deployment created successfully",
        deployment=deployment_to_dict(deployment),
    ), 201


@blueprint.route("/latest", methods=["GET"])
@login_required
def latest_deployments():
    limit = int_param("limit", 5, minimum=1, maximum=50) or 5
    rows = list(
        db.session.scalars(
            select(Deployment).order_by(Deployment.deployed_at.desc()).limit(limit)
        )
    )
    return jsonify([deployment_to_dict(row) for row in rows])


@blueprint.route("/<int:deployment_id>", methods=["GET"])
@login_required
def get_deployment(deployment_id: int):
    deployment = db.session.get(Deployment, deployment_id)
    if not deployment:
        return json_error("Deployment not found", 404)
    return jsonify(deployment_to_dict(deployment))


@blueprint.route("/<int:deployment_id>", methods=["PUT", "PATCH"])
@write_required
def update_deployment(deployment_id: int):
    deployment = db.session.get(Deployment, deployment_id)
    if not deployment:
        return json_error("Deployment not found", 404)

    data = _body()
    if not data:
        return json_error("Request body is required", 400)

    result = _validate(data)
    if result["errors"]:
        return json_error("Validation failed", 422, fields=result["errors"])

    changed = {}
    for field, value in result["values"].items():
        if getattr(deployment, field) != value:
            changed[field] = {"from": getattr(deployment, field), "to": value}
            setattr(deployment, field, value)

    if not changed:
        return success("No changes were supplied", deployment=deployment_to_dict(deployment))

    db.session.commit()
    audit.record(
        "deployment.updated",
        entity_type="deployment",
        entity_id=deployment.id,
        summary=f"Updated deployment {deployment.version}",
        detail=changed,
    )

    return success(
        "Deployment updated successfully",
        deployment=deployment_to_dict(deployment),
    )


@blueprint.route("/<int:deployment_id>/rollback", methods=["POST"])
@write_required
def rollback_deployment(deployment_id: int):
    deployment = db.session.get(Deployment, deployment_id)
    if not deployment:
        return json_error("Deployment not found", 404)

    if deployment.status == "Rolled Back":
        return json_error("Deployment is already rolled back", 409)

    previous_status = deployment.status
    deployment.status = "Rolled Back"
    deployment.rolled_back_at = datetime.now(UTC)
    db.session.commit()

    audit.record(
        "deployment.rolled_back",
        entity_type="deployment",
        entity_id=deployment.id,
        summary=f"Rolled back deployment {deployment.version}",
        detail={"from": previous_status, "to": "Rolled Back"},
    )

    return success(
        "Deployment rolled back successfully",
        deployment=deployment_to_dict(deployment),
    )


@blueprint.route("/<int:deployment_id>/incidents", methods=["GET"])
@login_required
def deployment_incidents(deployment_id: int):
    deployment = db.session.get(Deployment, deployment_id)
    if not deployment:
        return json_error("Deployment not found", 404)

    rows = list(
        db.session.scalars(
            select(Incident)
            .where(Incident.application_id == deployment.application_id)
            .order_by(Incident.detected_at.desc())
            .limit(25)
        )
    )

    return jsonify(
        {
            "deployment_id": deployment.id,
            "linked_incidents": [
                {
                    "id": incident.id,
                    "title": incident.title,
                    "severity": incident.severity,
                    "status": incident.status,
                    "detected_at": (
                        incident.detected_at.isoformat() if incident.detected_at else None
                    ),
                }
                for incident in rows
            ],
        }
    )


@blueprint.route("/<int:deployment_id>", methods=["DELETE"])
@write_required
def delete_deployment(deployment_id: int):
    deployment = db.session.get(Deployment, deployment_id)
    if not deployment:
        return json_error("Deployment not found", 404)

    version = deployment.version
    db.session.delete(deployment)
    db.session.commit()

    audit.record(
        "deployment.deleted",
        entity_type="deployment",
        entity_id=deployment_id,
        summary=f"Deleted deployment {version}",
    )

    return success("Deployment deleted successfully")
