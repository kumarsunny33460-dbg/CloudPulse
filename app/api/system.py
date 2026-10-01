"""System endpoints: health probes, Prometheus metrics and runtime metadata."""

from __future__ import annotations

import logging
import platform
import time
from datetime import UTC, datetime

from extensions import db
from flask import Blueprint, Response, current_app, jsonify
from services import metrics, scheduler
from sqlalchemy import text

logger = logging.getLogger("cloudpulse.api.system")

blueprint = Blueprint("system_api", __name__)

START_TIME = time.time()


@blueprint.route("/health", methods=["GET"])
def health():
    """Liveness probe used by Docker, Render and Kubernetes."""
    logger.info("Health endpoint accessed")
    return jsonify({"status": "healthy"})


@blueprint.route("/health/ready", methods=["GET"])
def readiness():
    """Readiness probe: verifies the database is actually reachable."""
    database_ok = True
    detail = "ok"

    try:
        db.session.execute(text("SELECT 1"))
    except Exception as error:  # pragma: no cover - depends on environment
        database_ok = False
        detail = str(error)
        logger.exception("Readiness check failed")

    payload = {
        "status": "ready" if database_ok else "degraded",
        "database": {"status": "connected" if database_ok else "unreachable", "detail": detail},
        "scheduler": scheduler.status(),
        "uptime_seconds": round(time.time() - START_TIME, 2),
        "checked_at": datetime.now(UTC).isoformat(),
    }

    return jsonify(payload), 200 if database_ok else 503


@blueprint.route("/health/live", methods=["GET"])
def liveness():
    return jsonify({"status": "alive"})


@blueprint.route("/api/cicd", methods=["GET"])
def cicd():
    """Endpoint used by CI/CD pipelines to verify a deployment is live."""
    logger.info("CI/CD verification endpoint accessed")
    return jsonify(
        {
            "status": "success",
            "message": "CloudPulse CI/CD pipeline is working",
            "version": metrics.APP_VERSION,
            "commit": current_app.config.get("GIT_COMMIT", "unknown"),
            "environment": current_app.config.get("ENV_NAME", "development"),
        }
    )


@blueprint.route("/metrics", methods=["GET"])
def prometheus_metrics():
    """Prometheus scrape endpoint (no authentication, network-restricted)."""
    return Response(metrics.render(), mimetype="text/plain; version=0.0.4; charset=utf-8")


@blueprint.route("/api/system", methods=["GET"])
def system_info():
    return jsonify(
        {
            "application": "CloudPulse",
            "version": metrics.APP_VERSION,
            "environment": current_app.config.get("ENV_NAME", "development"),
            "python": platform.python_version(),
            "platform": platform.system(),
            "uptime_seconds": round(time.time() - START_TIME, 2),
            "scheduler": scheduler.status(),
            "database": _database_dialect(),
        }
    )


def _database_dialect() -> str:
    try:
        return db.engine.dialect.name
    except Exception:  # pragma: no cover - defensive
        return "unknown"
