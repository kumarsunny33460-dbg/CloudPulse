"""Data retention jobs.

Every table that grows without bound gets a prune here. Health checks already
had one; audit logs, notification outbox rows and spent auth tokens are handled
here so a long running instance does not fill its disk with history nobody
reads.

Each function is idempotent and safe to run concurrently: deletes are bounded
by a retention window derived from configuration, and nothing runs unless the
window is greater than zero.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from extensions import db
from flask import current_app
from models import AuditLog, Notification
from sqlalchemy import delete

logger = logging.getLogger("cloudpulse.retention")


def _setting(name: str, default):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def _cutoff(retention_days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=retention_days)


def _prune(model, column, retention_days: int, label: str) -> int:
    if retention_days <= 0:
        return 0

    result = db.session.execute(
        delete(model).where(column < _cutoff(retention_days))
    )
    removed = result.rowcount or 0

    if removed:
        logger.info("Retention | Removed %s %s rows older than %s days",
                    removed, label, retention_days)

    return removed


def prune_audit_logs(retention_days: int | None = None) -> int:
    if retention_days is None:
        retention_days = int(_setting("AUDIT_LOG_RETENTION_DAYS", 180))
    return _prune(AuditLog, AuditLog.created_at, retention_days, "audit log")


def prune_notifications(retention_days: int | None = None) -> int:
    if retention_days is None:
        retention_days = int(_setting("NOTIFICATION_RETENTION_DAYS", 30))
    return _prune(Notification, Notification.created_at, retention_days, "notification")


class RetentionError(RuntimeError):
    """Raised when a retention job fails partway through.

    The endpoint maps this to a 500. Returning an empty summary instead made
    ``sum({}.values())`` evaluate to ``0``, so a total failure was reported to
    the operator as ``200 {"message": "Pruned 0 records"}`` and the backlog
    looked cleared.
    """


def prune_all() -> dict:
    """Run every retention job as one transaction and report the totals.

    The deletes are staged together and committed once. The individual
    helpers used to commit internally, which meant a later failure left earlier
    deletes already durable -- a partially pruned database with no way to tell
    which half ran.
    """
    from services import accounts, health, ratelimit

    summary: dict[str, int] = {}

    try:
        summary["health_checks"] = health.prune_health_checks(commit=False)
        summary["audit_logs"] = prune_audit_logs()
        summary["notifications"] = prune_notifications()
        summary["auth_tokens"] = accounts.prune_expired_tokens(commit=False)
        summary["rate_limit_keys"] = ratelimit.purge_expired()

        db.session.commit()
    except Exception as error:
        db.session.rollback()
        logger.exception("Retention job failed; no rows were removed")
        raise RetentionError(str(error)) from error

    logger.info("Retention | Completed %s", summary)
    return summary
