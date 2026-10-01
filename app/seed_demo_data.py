"""Populate a local database with a realistic demo data set.

Usage
-----
    python app/seed_demo_data.py            # seed the default SQLite database
    python app/seed_demo_data.py --reset    # wipe CloudPulse data first

Everything is created through the service layer, so the demo dataset exercises
exactly the same rules as real usage: health checks, incident de-duplication,
auto-resolution, audit entries and the notification outbox.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from extensions import db  # noqa: E402
from models import (  # noqa: E402
    Application,
    AuditLog,
    AuthToken,
    Deployment,
    HealthCheck,
    Incident,
    Notification,
    User,
)
from services import health as health_service  # noqa: E402

from app import create_app  # noqa: E402

DEMO_TARGET_BASE = os.getenv("DEMO_TARGET_BASE", "http://127.0.0.1:5099")

SEED_APPLICATIONS = [
    {
        "name": "payments-api",
        "url": DEMO_TARGET_BASE + "/health",
        "environment": "Production",
        "description": "Card authorisation and settlement service",
        "expected_status_codes": "200-399",
    },
    {
        "name": "search-indexer",
        "url": DEMO_TARGET_BASE + "/healthz",
        "environment": "Production",
        "description": "Catalog indexing pipeline",
        "expected_body_keyword": "ok",
    },
    {
        "name": "web-storefront",
        "url": DEMO_TARGET_BASE + "/",
        "environment": "Staging",
        "description": "Customer facing storefront",
    },
    {
        "name": "batch-worker",
        "url": DEMO_TARGET_BASE + "/slow",
        "environment": "Development",
        "description": "Nightly reconciliation jobs, known to be slow",
    },
    {
        "name": "notification-gateway",
        "url": DEMO_TARGET_BASE + "/health",
        "environment": "Production",
        "description": "Email, SMS and push delivery",
    },
    {
        "name": "legacy-report-service",
        "url": DEMO_TARGET_BASE + "/status/404",
        "environment": "Development",
        "description": "Decommissioned endpoint, expected to answer 404",
        "expected_status_codes": "404",
    },
]

SEED_DEPLOYMENTS = [
    ("v2.4.0", "Production", "Successful", "9f2c1ab4d5e6f7a8", 142, "Adds idempotency keys to charge"),
    ("v2.3.1", "Production", "Rolled Back", "1a2b3c4d5e6f7a8", 98, "Reverted: latency regression"),
    ("v2.3.0", "Production", "Successful", "5d4c3b2a19080706", 131, "Migrates the ledger table"),
    ("v1.9.0", "Staging", "Failed", "a1b2c3d4e5f60718", 47, "Rollout timed out on the canary"),
    ("v1.8.2", "Production", "Successful", "ff00ee11dd22cc33", 126, "Security patches"),
]

SEED_INCIDENTS = [
    ("payments-api", "Checkout latency above 4 seconds", "Critical", "Investigating",
     "p99 latency breached the SLO for 12 minutes across the checkout funnel", "sre-oncall"),
    ("search-indexer", "Search results stale for the EU region", "Medium", "Open",
     "Indexing lag is 40 minutes behind the ingestion pipeline", None),
    ("notification-gateway", "Webhook delivery retries exhausted", "High", "Open",
     "Slack and Teams webhooks are returning 429", "platform-team"),
]


def _mock_response(status: int = 200, body: bytes = b"ok") -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.status = status
    response.read = MagicMock(return_value=body)
    return response


def reset_data() -> None:
    """Wipe everything the demo owns, leaving user accounts alone.

    Notification and audit rows are cleared too. They are not deleted by the
    application/incident cascade, so without this the outbox would keep
    pointing at incidents that no longer exist and every retry would 409.
    """
    for model in (
        Notification,
        AuditLog,
        AuthToken,
        HealthCheck,
        Incident,
        Deployment,
        Application,
    ):
        db.session.query(model).delete()
    db.session.commit()
    print("Existing CloudPulse data removed (accounts were left untouched).")


def seed_history(application: Application, days: int = 7) -> None:
    """Backfill a realistic availability history for the charts."""
    now = datetime.now(UTC)
    rng = random.Random(application.name)

    for day_offset in range(days, 0, -1):
        for hour in range(24):
            checked_at = now - timedelta(days=day_offset, hours=now.hour - hour)
            if checked_at > now:
                continue

            # Occasional outage windows keep the availability realistic.
            is_outage = (day_offset == 2 and 9 <= hour <= 11) or (
                day_offset == 5 and hour == 3
            )

            if is_outage:
                status, http_status, latency, error = "DOWN", None, None, "Application is unreachable"
            else:
                latency = round(rng.uniform(28, 180), 2)
                if rng.random() > 0.97:
                    status, http_status, latency = "DEGRADED", 200, round(rng.uniform(1100, 2400), 2)
                else:
                    status, http_status = "UP", 200
                error = None if status == "UP" else "Slow response"

            db.session.add(
                HealthCheck(
                    application_id=application.id,
                    status=status,
                    http_status=http_status,
                    response_time=latency,
                    checked_at=checked_at,
                    error_message=error,
                )
            )

    # Roll the aggregate fields forward so the dashboard is not empty.
    checks = (
        db.session.query(HealthCheck)
        .filter(HealthCheck.application_id == application.id)
        .all()
    )
    total = len(checks)
    healthy = sum(1 for check in checks if check.status in ("UP", "DEGRADED"))
    failures = [check for check in checks if check.status == "DOWN"]

    application.uptime_percentage = round((healthy / total) * 100, 2) if total else 100.0
    application.consecutive_failures = 0
    application.last_health_status = checks[-1].status if checks else "UNKNOWN"
    application.last_checked_at = checks[-1].checked_at if checks else None
    latencies = [check.response_time for check in checks if check.response_time is not None]
    application.last_response_time = round(sum(latencies) / len(latencies), 2) if latencies else None
    application.last_http_status = checks[-1].http_status if checks else None

    for check in failures:
        db.session.add(
            HealthCheck(
                application_id=application.id,
                status="DOWN",
                checked_at=check.checked_at,
                error_message=check.error_message,
            )
        )

    db.session.commit()


def seed() -> None:
    created: dict[str, Application] = {}

    for spec in SEED_APPLICATIONS:
        existing = db.session.query(Application).filter_by(name=spec["name"]).first()
        if existing:
            print(f"  skip {spec['name']} (already registered)")
            created[spec["name"]] = existing
            continue

        application = Application(**spec)
        db.session.add(application)
        db.session.commit()
        created[spec["name"]] = application
        print(f"  + {application.name}")

    print("Backfilling health history (7 days)...")
    for application in created.values():
        seed_history(application)

    print("Recording deployments...")
    for index, (version, environment, status, commit, duration, changelog) in enumerate(
        SEED_DEPLOYMENTS
    ):
        target = list(created.values())[index % len(created)]
        db.session.add(
            Deployment(
                application_id=target.id,
                version=version,
                environment=environment,
                status=status,
                commit_hash=commit,
                changelog=changelog,
                duration_seconds=duration,
                deployed_by="release-bot",
                deployed_at=datetime.now(UTC) - timedelta(days=index * 2, hours=3),
                rolled_back_at=(
                    datetime.now(UTC) - timedelta(days=index * 2 - 1)
                    if status == "Rolled Back"
                    else None
                ),
            )
        )
    db.session.commit()

    print("Recording incidents...")
    now = datetime.now(UTC)
    for offset, (name, title, severity, status, description, assignee) in enumerate(
        SEED_INCIDENTS
    ):
        detected = now - timedelta(hours=offset * 5 + 1)
        db.session.add(
            Incident(
                application_id=created[name].id,
                title=title,
                description=description,
                severity=severity,
                status=status,
                source="monitoring" if offset == 0 else "manual",
                assignee=assignee,
                detected_at=detected,
                acknowledged_at=detected + timedelta(minutes=12) if status != "Open" else None,
                resolved_at=(
                    now - timedelta(minutes=offset * 20)
                    if severity == "Low" and offset > 0
                    else None
                ),
            )
        )
    db.session.commit()

    # A couple of resolved incidents so MTTR and SLA compliance have data.
    for index in range(4):
        target = list(created.values())[index % len(created)]
        detected = now - timedelta(days=index + 2, hours=6)
        resolved = detected + timedelta(minutes=45 + index * 30)
        db.session.add(
            Incident(
                application_id=target.id,
                title=f"Transient {target.name} outage #{index + 1}",
                description="Detected by the health check, resolved after the upstream recovered.",
                severity="High",
                status="Resolved",
                source="monitoring",
                root_cause="Upstream dependency returned 503 for six minutes.",
                detected_at=detected,
                acknowledged_at=detected + timedelta(minutes=3),
                resolved_at=resolved,
            )
        )
    db.session.commit()

    print("Running one live monitoring cycle...")
    with patch("services.health.urlopen", return_value=_mock_response()):
        summary = health_service.run_monitoring_cycle(force=True)
    print(f"  {summary['summary']}")
    print()
    print("For genuinely live checks against these targets, run the demo target")
    print("service in a second terminal:")
    print("    python tools/demo_targets.py")
    print("Then press 'Run all checks' in the Applications view.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete existing applications, incidents and deployments first",
    )
    arguments = parser.parse_args()

    application = create_app()
    print(f"Seeding CloudPulse ({application.config['ENV_NAME']})...")

    with application.app_context():
        if arguments.reset:
            reset_data()
        seed()

        print()
        print("Summary")
        print("  applications:", db.session.query(Application).count())
        print("  health checks:", db.session.query(HealthCheck).count())
        print("  incidents:    ", db.session.query(Incident).count())
        print("  deployments:  ", db.session.query(Deployment).count())
        print("  users:        ", db.session.query(User).count())
        print()
        print("Start the app with 'python app/app.py' and sign in.")


if __name__ == "__main__":
    main()
