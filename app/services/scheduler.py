"""Background monitoring scheduler.

Uses APScheduler when available and degrades to a small standard library thread
when it is not installed, so the monitoring loop never becomes a hard runtime
dependency of the API process.
"""

from __future__ import annotations

import logging
import threading
import time

from extensions import db
from flask import current_app

from services import health, metrics, retention

logger = logging.getLogger("cloudpulse.scheduler")

_scheduler = None
_lock = threading.Lock()


def _run_cycle() -> None:
    from app import app as flask_app  # local import avoids a circular import

    with flask_app.app_context():
        try:
            result = health.run_monitoring_cycle()
            metrics.mark_monitoring_run()
            logger.info(
                "Background monitoring cycle finished | Healthy: %s | Down: %s",
                result["summary"]["healthy"],
                result["summary"]["unhealthy"],
            )
        except Exception:  # pragma: no cover - defensive
            db.session.rollback()
            logger.exception("Background monitoring cycle failed")


def _run_retention() -> None:
    from app import app as flask_app

    with flask_app.app_context():
        try:
            retention.prune_all()
        except retention.RetentionError:
            # Already logged with a traceback inside prune_all, and the rows are
            # untouched because the job is one transaction.
            logger.exception("Retention job failed")


def start(app=None) -> bool:
    """Start the scheduler if it is enabled and not already running."""
    global _scheduler

    app = app or current_app

    if not app.config.get("ENABLE_SCHEDULER"):
        logger.info("Background scheduler disabled (ENABLE_SCHEDULER=false)")
        return False

    with _lock:
        if _scheduler is not None:
            return True

        interval = max(15, int(app.config.get("MONITOR_INTERVAL_SECONDS", 60)))
        retention_hours = max(1, int(app.config.get("RETENTION_JOB_HOURS", 6)))

        try:
            from apscheduler.schedulers.background import BackgroundScheduler

            scheduler = BackgroundScheduler(
                timezone=app.config.get("SCHEDULER_TIMEZONE", "UTC"),
                daemon=True,
            )
            scheduler.add_job(
                _run_cycle,
                "interval",
                seconds=interval,
                id="cloudpulse-monitor",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=interval,
            )
            scheduler.add_job(
                _run_retention,
                "interval",
                hours=retention_hours,
                id="cloudpulse-retention",
                max_instances=1,
                coalesce=True,
            )
            scheduler.start()
            _scheduler = scheduler
            logger.info(
                "Background scheduler started | interval: %ss | retention: %sh",
                interval,
                retention_hours,
            )
            return True

        except ImportError:
            logger.warning(
                "APScheduler is not installed, falling back to the standard "
                "library scheduler thread"
            )

        stop_event = threading.Event()
        thread = threading.Thread(
            target=_fallback_loop,
            args=(interval, retention_hours, stop_event),
            name="cloudpulse-monitor",
            daemon=True,
        )
        thread.start()
        _scheduler = stop_event
        logger.info("Fallback monitoring thread started | interval: %ss", interval)
        return True


def _fallback_loop(interval: int, retention_hours: int, stop_event: threading.Event) -> None:
    last_retention = time.time()

    while not stop_event.is_set():
        _run_cycle()

        if time.time() - last_retention >= retention_hours * 3600:
            _run_retention()
            last_retention = time.time()

        stop_event.wait(interval)


def shutdown() -> None:
    global _scheduler

    with _lock:
        if _scheduler is None:
            return

        if isinstance(_scheduler, threading.Event):
            _scheduler.set()
        else:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to stop the background scheduler")

        _scheduler = None
        logger.info("Background scheduler stopped")


def status() -> dict:
    running = _scheduler is not None
    return {
        "enabled": bool(_scheduler is not None),
        "running": running,
        "interval_seconds": (
            int(_scheduler.get_job("cloudpulse-monitor").trigger.interval.total_seconds())
            if running and hasattr(_scheduler, "get_job")
            else None
        ),
    }
