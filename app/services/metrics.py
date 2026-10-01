"""Prometheus exposition and in-process HTTP instrumentation.

The exporter is implemented directly against the Prometheus text exposition
format so CloudPulse does not need an extra client library at runtime. All
database derived values are computed at scrape time, which keeps the numbers
correct after a restart and avoids stale in-memory state.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict

from extensions import db
from models import (
    Application,
    Deployment,
    HealthCheck,
    Incident,
)
from sqlalchemy import func, select

APP_VERSION = "1.0.0"
DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_lock = threading.Lock()
_http_requests: dict[tuple[str, str, str], int] = defaultdict(int)
_http_duration_buckets: dict[tuple[str, str], dict[float, int]] = defaultdict(
    lambda: defaultdict(int)
)
_http_duration_sum: dict[tuple[str, str], float] = defaultdict(float)
_http_duration_count: dict[tuple[str, str], int] = defaultdict(int)
_state = {"monitoring_started_at": None, "monitoring_runs": 0, "last_run_timestamp": None}


def _now() -> float:
    return time.time()


def reset() -> None:
    """Clear in-process counters (used by the test suite)."""
    with _lock:
        _http_requests.clear()
        _http_duration_buckets.clear()
        _http_duration_sum.clear()
        _http_duration_count.clear()
        _state["monitoring_runs"] = 0
        _state["last_run_timestamp"] = None


def record_request(route: str, method: str, status: int, duration_seconds: float) -> None:
    key = (route, method, str(status))
    with _lock:
        _http_requests[key] += 1
        _http_duration_sum[(route, method)] += duration_seconds
        _http_duration_count[(route, method)] += 1
        for bucket in DURATION_BUCKETS:
            if duration_seconds <= bucket:
                _http_duration_buckets[(route, method)][bucket] += 1


def mark_monitoring_run() -> None:
    with _lock:
        _state["monitoring_runs"] += 1
        _state["last_run_timestamp"] = _now()
        if _state["monitoring_started_at"] is None:
            _state["monitoring_started_at"] = _now()


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _labels(pairs: dict[str, str]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{key}="{_escape(value)}"' for key, value in sorted(pairs.items()))
    return "{" + inner + "}"


def _metric(
    name: str,
    help_text: str,
    metric_type: str,
    samples: list[tuple[dict[str, str] | None, object]],
) -> list[str]:
    """Render a HELP/TYPE header plus one ``name{labels} value`` line per sample."""
    if not samples:
        return []
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"]
    for labels, value in samples:
        rendered = _labels(labels) if labels else ""
        lines.append(f"{name}{rendered} {value}")
    return lines


def render() -> str:
    """Build the full Prometheus exposition payload."""
    lines: list[str] = []

    # ---------------------------------------------------------------- build
    lines += _metric(
        "cloudpulse_build_info",
        "Static build information for the running CloudPulse instance.",
        "gauge",
        [
            (
                {
                    "version": APP_VERSION,
                    "commit": os.getenv("GIT_COMMIT_SHA", "unknown"),
                    "environment": os.getenv("APP_ENV", "development"),
                },
                1,
            )
        ],
    )

    # --------------------------------------------------------- applications
    applications = list(db.session.scalars(select(Application).order_by(Application.id)))

    up_samples: list[tuple[dict, object]] = []
    response_samples: list[tuple[dict, object]] = []
    uptime_samples: list[tuple[dict, object]] = []
    failure_samples: list[tuple[dict, object]] = []
    info_samples: list[tuple[dict, object]] = []

    for application in applications:
        labels = {
            "application": application.name,
            "environment": application.environment,
            "id": str(application.id),
        }
        up_samples.append((labels, 1 if application.last_health_status == "UP" else 0))
        response_samples.append((labels, application.last_response_time or 0))
        uptime_samples.append((labels, (application.uptime_percentage or 0) / 100))
        failure_samples.append((labels, application.consecutive_failures or 0))
        info_samples.append(({**labels, "url": application.url}, 1))

    lines += _metric(
        "cloudpulse_application_up",
        "1 when the most recent health check succeeded, 0 otherwise.",
        "gauge",
        up_samples,
    )
    lines += _metric(
        "cloudpulse_application_response_time_ms",
        "Latency of the most recent health check in milliseconds.",
        "gauge",
        response_samples,
    )
    lines += _metric(
        "cloudpulse_application_uptime_ratio",
        "Rolling availability ratio reported by the monitoring engine.",
        "gauge",
        uptime_samples,
    )
    lines += _metric(
        "cloudpulse_application_consecutive_failures",
        "Number of consecutive failed health checks.",
        "gauge",
        failure_samples,
    )
    lines += _metric(
        "cloudpulse_application_info",
        "Static labels for each monitored application.",
        "gauge",
        info_samples,
    )

    # -------------------------------------------------------- health checks
    check_rows = db.session.execute(
        select(HealthCheck.status, func.count(HealthCheck.id)).group_by(HealthCheck.status)
    ).all()
    lines += _metric(
        "cloudpulse_health_checks_recorded",
        "Health check results currently stored per status.",
        "gauge",
        [({"status": status}, count) for status, count in check_rows],
    )

    average, slowest = db.session.execute(
        select(func.avg(HealthCheck.response_time), func.max(HealthCheck.response_time))
    ).one()
    lines += _metric(
        "cloudpulse_health_check_response_time_ms",
        "Aggregate health check latency across all recorded checks.",
        "gauge",
        [
            ({"stat": "avg"}, round(average, 2) if average else 0),
            ({"stat": "max"}, round(slowest, 2) if slowest else 0),
        ],
    )

    # ------------------------------------------------------------ incidents
    incident_rows = db.session.execute(
        select(Incident.severity, Incident.status, func.count(Incident.id))
        .group_by(Incident.severity, Incident.status)
    ).all()
    lines += _metric(
        "cloudpulse_incidents",
        "Incidents grouped by severity and status.",
        "gauge",
        [({"severity": severity, "status": status}, count) for severity, status, count in incident_rows],
    )

    open_total = (
        db.session.scalar(select(func.count(Incident.id)).where(Incident.status != "Resolved"))
        or 0
    )
    lines += _metric(
        "cloudpulse_incidents_open",
        "Number of incidents that are not resolved.",
        "gauge",
        [(None, open_total)],
    )

    # ---------------------------------------------------------- deployments
    deployment_rows = db.session.execute(
        select(Deployment.environment, Deployment.status, func.count(Deployment.id))
        .group_by(Deployment.environment, Deployment.status)
    ).all()
    lines += _metric(
        "cloudpulse_deployments",
        "Recorded deployments grouped by environment and status.",
        "gauge",
        [
            ({"environment": environment, "status": status}, count)
            for environment, status, count in deployment_rows
        ],
    )

    # ------------------------------------------------------------- runtime
    with _lock:
        request_samples: list[tuple[dict, object]] = [
            ({"method": method, "route": route, "status": status}, count)
            for (route, method, status), count in sorted(_http_requests.items())
        ]
        duration_buckets: list[tuple[dict, object]] = []
        duration_sum: list[tuple[dict, object]] = []
        duration_count: list[tuple[dict, object]] = []

        for (route, method), total in sorted(_http_duration_count.items()):
            labels = {"method": method, "route": route}
            observed = _http_duration_buckets[(route, method)]
            for bucket in DURATION_BUCKETS:
                duration_buckets.append(
                    ({"le": str(bucket), **labels}, observed.get(bucket, 0))
                )
            duration_buckets.append(({"le": "+Inf", **labels}, total))
            duration_sum.append((labels, round(_http_duration_sum[(route, method)], 6)))
            duration_count.append((labels, total))

        runs = _state["monitoring_runs"]
        last_run = _state["last_run_timestamp"] or 0
        started = _state["monitoring_started_at"] or _now()

    lines += _metric(
        "cloudpulse_http_requests_total",
        "HTTP requests handled by this process, labelled by route.",
        "counter",
        request_samples,
    )
    lines += _metric(
        "cloudpulse_http_request_duration_seconds_bucket",
        "HTTP request latency distribution, labelled by route.",
        "histogram",
        duration_buckets,
    )
    lines += _metric(
        "cloudpulse_http_request_duration_seconds_sum",
        "Total HTTP request latency observed, labelled by route.",
        "gauge",
        duration_sum,
    )
    lines += _metric(
        "cloudpulse_http_request_duration_seconds_count",
        "Number of HTTP requests included in the latency histogram.",
        "gauge",
        duration_count,
    )
    lines += _metric(
        "cloudpulse_monitoring_runs_total",
        "Monitoring cycles executed by this process.",
        "counter",
        [(None, runs)],
    )
    lines += _metric(
        "cloudpulse_monitoring_last_run_timestamp_seconds",
        "Unix timestamp of the last completed monitoring cycle.",
        "gauge",
        [(None, last_run)],
    )
    lines += _metric(
        "cloudpulse_process_uptime_seconds",
        "Seconds since the monitoring engine started in this process.",
        "gauge",
        [(None, round(_now() - started, 2))],
    )
    lines += _metric(
        "cloudpulse_scrape_timestamp_seconds",
        "Unix timestamp at which this scrape was generated.",
        "gauge",
        [(None, round(_now(), 3))],
    )

    return "\n".join(lines) + "\n"
