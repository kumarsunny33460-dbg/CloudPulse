"""Shared helpers for the CloudPulse REST API blueprints."""

from __future__ import annotations

import csv
import io
import logging
from functools import wraps

from extensions import db
from flask import jsonify, request, session
from models import Application

logger = logging.getLogger("cloudpulse.api")

WRITE_ROLES = ("Admin", "Editor")
READ_ROLES = ("Admin", "Editor", "Viewer")


def json_error(message: str, status: int = 400, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


def success(message: str, **extra):
    payload = {"message": message}
    payload.update(extra)
    return jsonify(payload)


def write_required(function):
    """Allow only Admin and Editor roles to mutate data.

    Authentication stays owned by ``app.login_required``; this decorator only
    adds the authorisation layer on top of an authenticated session.
    """
    from security import login_required

    @wraps(function)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return json_error("Authentication required", 401)

        role = session.get("role")
        if role not in WRITE_ROLES:
            return json_error(
                "Insufficient permissions for this action",
                403,
                required_roles=list(WRITE_ROLES),
                current_role=role,
            )

        return function(*args, **kwargs)

    return login_required(decorated)


def bool_param(name: str, default: bool | None = None) -> bool | None:
    raw = request.args.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def int_param(name: str, default: int | None = None, minimum: int | None = None,
              maximum: int | None = None) -> int | None:
    raw = request.args.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def pagination_args() -> tuple[int, int, int]:
    default_size = 25
    max_size = 200
    try:
        default_size = int(current_app_default("DEFAULT_PAGE_SIZE"))
        max_size = int(current_app_default("MAX_PAGE_SIZE"))
    except Exception:  # pragma: no cover - outside app context
        pass

    page = int_param("page", 1, minimum=1) or 1
    per_page = int_param("per_page", int_param("limit", default_size), minimum=1, maximum=max_size)
    return page, per_page, (page - 1) * per_page


def current_app_default(key: str, fallback=None):
    from flask import current_app

    return current_app.config.get(key, fallback)


def wants_pagination() -> bool:
    """Paginated envelopes are opt-in so the original array contract survives."""
    if bool_param("paginated", False):
        return True
    return any(key in request.args for key in ("page", "per_page", "limit"))


def envelope(items: list, total: int, page: int, per_page: int, **extra) -> dict:
    total_pages = (total + per_page - 1) // per_page if per_page else 1
    payload = {
        "data": items,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_previous": page > 1,
        },
    }
    payload.update(extra)
    return payload


def load_application_or_404(application_id: int):
    return db.session.get(Application, application_id)


def rows_to_csv(headers: list[str], rows: list[list]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue()


def csv_response(filename: str, headers: list[str], rows: list[list]):
    from flask import Response

    return Response(
        rows_to_csv(headers, rows),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
