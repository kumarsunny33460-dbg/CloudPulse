"""Application health probing and monitoring cycle orchestration."""

from __future__ import annotations

import logging
import socket
import ssl
import time
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from extensions import db
from flask import current_app
from models import Application, HealthCheck
from sqlalchemy import delete, select

from services import incidents

logger = logging.getLogger("cloudpulse.health")

UP = "UP"
DOWN = "DOWN"
DEGRADED = "DEGRADED"
UNKNOWN = "UNKNOWN"

SLA_TARGET_MS = 1000.0


def _setting(name: str, default):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def validate_url(url: str) -> str | None:
    """Return an error message when the URL cannot be monitored."""
    if not url or not url.strip():
        return "Application URL is required"

    candidate = url.strip()
    parsed = urlparse(candidate)
    allowed = _setting("HEALTH_CHECK_ALLOWED_SCHEMES", ("http", "https"))

    if parsed.scheme.lower() not in allowed:
        return f"URL scheme '{parsed.scheme or 'missing'}' is not allowed"
    if not parsed.netloc:
        return "URL must include a host name"

    # A pasted URL with a stray space fails deep inside urllib with a message
    # about "control characters", which sends an operator looking for a
    # non-existent encoding bug. Name the actual problem instead.
    if any(char.isspace() for char in candidate):
        return (
            "URL contains whitespace. Remove the spaces, including any that "
            "were pasted at the end of the path."
        )

    return None


def _describe_failure(reason) -> str:
    """Turn a low level socket or TLS error into an actionable message."""
    text = str(reason)

    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return (
            "TLS certificate verification failed. Install the issuing CA in the "
            "system trust store, or set HEALTH_CHECK_VERIFY_TLS=false when "
            "monitoring through a TLS-intercepting proxy."
        )
    if isinstance(reason, ssl.SSLError):
        return f"TLS handshake failed: {reason}"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"Timed out after the configured timeout: {reason}"
    if isinstance(reason, socket.gaierror):
        return f"DNS lookup failed: {reason}"
    if isinstance(reason, ConnectionRefusedError):
        return "Connection refused: nothing is listening on that address"
    if isinstance(reason, OSError) and reason.errno == 10060:
        return "Connection timed out: the host did not answer"

    return f"Application is unreachable: {reason}"


def _ssl_context():
    """Return an SSL context, or ``None`` when TLS verification is disabled.

    Corporate networks often intercept TLS with a proxy whose root certificate
    is only installed in the system store. ``HEALTH_CHECK_VERIFY_TLS=false``
    lets CloudPulse probe through such a proxy; the trade-off is that a
    certificate error will no longer fail the check.
    """
    if bool(_setting("HEALTH_CHECK_VERIFY_TLS", True)):
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def build_request(application: Application) -> Request:
    headers = {"User-Agent": _setting("HEALTH_CHECK_USER_AGENT", "CloudPulse-Monitor/1.0")}
    headers.update(application.parsed_request_headers)
    return Request(application.url.strip(), method=application.http_method, headers=headers)


def probe(application: Application) -> dict:
    """Perform a single network probe and return a raw, unpersisted result.

    This function never touches the database so it can be unit tested and
    reused by ad-hoc "check now" endpoints.
    """
    checked_at = _now()
    base = {
        "application_id": application.id,
        "name": application.name,
        "url": application.url,
        "environment": application.environment,
        "checked_at": checked_at.isoformat(),
    }

    url_error = validate_url(application.url)
    if url_error:
        return {
            **base,
            "health": DOWN,
            "response_time_ms": None,
            "http_status": None,
            "message": url_error,
        }

    timeout = application.timeout_seconds or 5
    max_bytes = _setting("HEALTH_CHECK_MAX_RESPONSE_BYTES", 65536)
    accepted = application.accepted_status_codes

    start = time.perf_counter()
    context = _ssl_context()

    try:
        with urlopen(build_request(application), timeout=timeout, context=context) as response:
            body = b""
            keyword = application.expected_body_keyword
            if keyword:
                body = response.read(max_bytes)

            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            status_code = response.status

            if status_code not in accepted:
                return {
                    **base,
                    "health": DOWN,
                    "response_time_ms": elapsed_ms,
                    "http_status": status_code,
                    "message": (
                        f"Unexpected HTTP status {status_code} "
                        f"(expected {application.expected_status_codes})"
                    ),
                }

            if keyword:
                text = body.decode("utf-8", errors="replace")
                if keyword.lower() not in text.lower():
                    return {
                        **base,
                        "health": DOWN,
                        "response_time_ms": elapsed_ms,
                        "http_status": status_code,
                        "message": f"Response body did not contain '{keyword}'",
                    }

            health = UP if elapsed_ms <= SLA_TARGET_MS else DEGRADED
            message = (
                "Application is reachable"
                if health == UP
                else f"Slow response ({elapsed_ms}ms exceeds {int(SLA_TARGET_MS)}ms)"
            )

            return {
                **base,
                "health": health,
                "response_time_ms": elapsed_ms,
                "http_status": status_code,
                "message": message,
            }

    except HTTPError as error:
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        status_code = error.code

        if status_code in accepted:
            return {
                **base,
                "health": UP,
                "response_time_ms": elapsed_ms,
                "http_status": status_code,
                "message": "Application responded within accepted status codes",
            }

        return {
            **base,
            "health": DOWN,
            "response_time_ms": elapsed_ms,
            "http_status": status_code,
            "message": f"Application returned HTTP {status_code}",
        }

    except (URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        message = _describe_failure(reason)
        logger.error(
            "Application unreachable | Application: %s | URL: %s | Reason: %s",
            application.name,
            application.url,
            message,
        )
        return {
            **base,
            "health": DOWN,
            "response_time_ms": None,
            "http_status": None,
            "message": message,
        }

    except ssl.SSLError as error:
        message = _describe_failure(error)
        logger.error(
            "TLS failure | Application: %s | URL: %s | Reason: %s",
            application.name,
            application.url,
            message,
        )
        return {
            **base,
            "health": DOWN,
            "response_time_ms": None,
            "http_status": None,
            "message": message,
        }

    except Exception as error:  # pragma: no cover - defensive
        logger.exception(
            "Unexpected health check error | Application: %s", application.name
        )
        return {
            **base,
            "health": DOWN,
            "response_time_ms": None,
            "http_status": None,
            "message": f"Health check failed: {error}",
        }


def _update_application_state(application: Application, result: dict) -> None:
    checked_at = _as_aware(datetime.fromisoformat(result["checked_at"]))
    healthy = result["health"] == UP

    application.last_checked_at = checked_at
    application.last_health_status = result["health"]
    application.last_http_status = result["http_status"]
    application.last_response_time = result["response_time_ms"]

    if healthy:
        application.consecutive_failures = 0
    else:
        application.consecutive_failures = (application.consecutive_failures or 0) + 1

    application.uptime_percentage = _recalculate_uptime(application)


def _recalculate_uptime(application: Application) -> float:
    """Availability over the most recent checks, not since the last failure.

    A DEGRADED service is still answering, so it counts as available. Toggling
    between 100 and 0 on consecutive checks would make the reported uptime
    useless, so it is derived from a rolling window of recent results instead.
    """
    window = max(10, int(_setting("UPTIME_WINDOW_CHECKS", 200)))

    recent = list(
        db.session.scalars(
            select(HealthCheck.status)
            .where(HealthCheck.application_id == application.id)
            .order_by(HealthCheck.checked_at.desc(), HealthCheck.id.desc())
            .limit(window)
        )
    )

    if not recent:
        return 100.0 if application.last_health_status in (UP, DEGRADED) else 0.0

    available = sum(1 for status in recent if status in (UP, DEGRADED))
    return round((available / len(recent)) * 100, 2)


def _persist_result(application: Application, result: dict) -> HealthCheck:
    health_check = HealthCheck(
        application_id=application.id,
        status=result["health"],
        http_status=result["http_status"],
        response_time=result["response_time_ms"],
        checked_at=_as_aware(datetime.fromisoformat(result["checked_at"])),
        error_message=None if result["health"] == UP else result["message"],
    )
    db.session.add(health_check)
    return health_check


def check_application_health(
    application: Application,
    *,
    create_incident: bool | None = None,
    commit: bool = True,
) -> dict:
    """Probe an application, persist the result and reconcile incidents."""
    result = probe(application)

    # Persist first so the rolling uptime window includes this check.
    health_check = _persist_result(application, result)
    _update_application_state(application, result)

    incident = None
    if result["health"] == DOWN:
        if create_incident is None:
            create_incident = bool(_setting("AUTO_CREATE_INCIDENTS", True))
        if create_incident:
            incident = incidents.create_incident_if_needed(application, result)
            if incident is not None:
                health_check.triggered_incident_id = incident.id
    else:
        if bool(_setting("AUTO_RESOLVE_INCIDENTS", True)):
            incident = incidents.auto_resolve_for_application(
                application,
                note=(
                    f"Automatically resolved: {application.name} responded "
                    f"with {result['http_status']} after a failed check."
                ),
            )
            if incident is not None:
                health_check.triggered_incident_id = incident.id

    if commit:
        db.session.commit()
    else:
        db.session.flush()

    return result


def is_due(application: Application, now: datetime | None = None) -> bool:
    """Return True when the application is due for another scheduled check."""
    if application.is_paused or application.status != "Operational":
        return False
    if not application.last_checked_at:
        return True
    last = _as_aware(application.last_checked_at)
    interval = application.check_interval_seconds or 60
    return (now or _now()) >= last + timedelta(seconds=interval)


def run_monitoring_cycle(force: bool = False) -> dict:
    """Run one monitoring pass over every application.

    Returns the same summary shape the dashboard expects.
    """
    started = time.perf_counter()
    now = _now()

    query = select(Application).order_by(Application.id.desc())
    if not force:
        applications = [app for app in db.session.scalars(query) if is_due(app, now)]
    else:
        applications = list(db.session.scalars(query))

    results: list[dict] = []
    created_incidents: list[int] = []
    resolved_incidents: list[int] = []

    threshold = int(_setting("MONITOR_DEGRADED_AFTER_CHECKS", 2))

    for application in applications:
        logger.info(
            "Checking application health | Application: %s | URL: %s",
            application.name,
            application.url,
        )

        if (
            threshold > 1
            and application.last_health_status == UP
            and (application.consecutive_failures or 0) + 1 < threshold
        ):
            # Debounce: a single blip should not open an incident yet.
            result = probe(application)
            _persist_result(application, result)
            _update_application_state(application, result)
            results.append(result)
            logger.info(
                "Debounced failure recorded | Application: %s | Failure %s/%s",
                application.name,
                application.consecutive_failures,
                threshold,
            )
            continue

        result = check_application_health(application)
        results.append(result)

        logger.info(
            "Health check result | Application: %s | Status: %s | Response: %sms",
            application.name,
            result["health"],
            result["response_time_ms"],
        )

        for incident in application.incidents:
            resolved_at = _as_aware(incident.resolved_at)
            if (
                incident.status == "Resolved"
                and resolved_at
                and resolved_at >= (now - timedelta(minutes=5))
            ):
                resolved_incidents.append(incident.id)

    healthy = [item for item in results if item["health"] == UP]
    degraded = [item for item in results if item["health"] == DEGRADED]
    down = [item for item in results if item["health"] == DOWN]

    open_incidents = incidents.count_open()

    if created_incidents:
        logger.warning("Incidents opened during cycle: %s", created_incidents)

    summary = {
        "total": len(results),
        "healthy": len(healthy),
        "degraded": len(degraded),
        "unhealthy": len(down),
        "open_incidents": open_incidents,
        "checked_at": now.isoformat(),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "triggered_incidents": created_incidents,
        "resolved_incidents": resolved_incidents,
    }

    logger.info("Monitoring cycle completed | %s", summary)
    return {"summary": summary, "applications": results}


def health_history(
    application: Application,
    *,
    limit: int = 50,
    hours: int | None = None,
    offset: int = 0,
) -> dict:
    """Return health check history with an availability summary."""
    limit = max(1, min(int(limit or 50), 1000))

    query = select(HealthCheck).where(HealthCheck.application_id == application.id)

    if hours:
        cutoff = _now() - timedelta(hours=int(hours))
        query = query.where(HealthCheck.checked_at >= cutoff)

    query = query.order_by(HealthCheck.checked_at.desc())
    checks = list(db.session.scalars(query.limit(limit + 1).offset(offset)))
    has_more = len(checks) > limit
    checks = checks[:limit]

    total = len(checks)
    up = sum(1 for check in checks if check.status == UP)
    degraded = sum(1 for check in checks if check.status == DEGRADED)
    down = total - up - degraded
    ok = up + degraded

    response_times = [
        check.response_time
        for check in checks
        if check.response_time is not None
    ]

    return {
        "application_id": application.id,
        "application_name": application.name,
        "summary": {
            "total_checks": total,
            "up_checks": up,
            "degraded_checks": degraded,
            "down_checks": down,
            "uptime_percentage": round((ok / total) * 100, 2) if total else 0,
            "avg_response_time": (
                round(sum(response_times) / len(response_times), 2)
                if response_times
                else None
            ),
            "min_response_time": min(response_times) if response_times else None,
            "max_response_time": max(response_times) if response_times else None,
        },
        "latest_status": checks[0].status if checks else (application.last_health_status or "N/A"),
        "has_more": has_more,
        "history": [
            {
                "id": check.id,
                "status": check.status,
                "http_status": check.http_status,
                "response_time": check.response_time,
                "checked_at": _as_aware(check.checked_at).isoformat(),
                "error_message": check.error_message,
                "triggered_incident_id": check.triggered_incident_id,
            }
            for check in checks
        ],
    }


def uptime_report(application: Application, days: int = 7) -> dict:
    """Aggregate daily availability for an application over a time window."""
    days = max(1, int(days))
    since = _now() - timedelta(days=days)

    checks = list(
        db.session.scalars(
            select(HealthCheck)
            .where(
                HealthCheck.application_id == application.id,
                HealthCheck.checked_at >= since,
            )
            .order_by(HealthCheck.checked_at.asc())
        )
    )

    buckets: dict[str, dict] = {}
    for offset in range(days):
        day = (since + timedelta(days=offset)).date().isoformat()
        buckets[day] = {"date": day, "total": 0, "ok": 0, "response_time_sum": 0.0,
                        "response_time_count": 0}

    for check in checks:
        key = _as_aware(check.checked_at).date().isoformat()
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["total"] += 1
        if check.status in (UP, DEGRADED):
            bucket["ok"] += 1
        if check.response_time is not None:
            bucket["response_time_sum"] += check.response_time
            bucket["response_time_count"] += 1

    series = []
    for bucket in sorted(buckets.values(), key=lambda item: item["date"]):
        total = bucket["total"]
        series.append(
            {
                "date": bucket["date"],
                "total_checks": total,
                "uptime_percentage": round((bucket["ok"] / total) * 100, 2) if total else None,
                "avg_response_time": (
                    round(bucket["response_time_sum"] / bucket["response_time_count"], 2)
                    if bucket["response_time_count"]
                    else None
                ),
            }
        )

    return {
        "application_id": application.id,
        "application_name": application.name,
        "days": days,
        "series": series,
    }


def prune_health_checks(retention_days: int | None = None, *, commit: bool = True) -> int:
    """Delete health check rows older than the retention window.

    ``commit=False`` lets :func:`services.retention.prune_all` stage this delete
    alongside the others so the whole job is one transaction. Committing here
    made an earlier delete durable even when a later step failed.
    """
    if retention_days is None:
        retention_days = int(_setting("HEALTH_CHECK_RETENTION_DAYS", 30))

    if retention_days <= 0:
        return 0

    cutoff = _now() - timedelta(days=retention_days)
    result = db.session.execute(
        delete(HealthCheck).where(HealthCheck.checked_at < cutoff)
    )
    removed = result.rowcount or 0

    if removed:
        if commit:
            db.session.commit()
        logger.info("Pruned %s health check rows older than %s days", removed, retention_days)

    return removed
