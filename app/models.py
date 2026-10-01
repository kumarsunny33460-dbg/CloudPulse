"""SQLAlchemy models for CloudPulse.

Conventions
-----------
* Every timestamp column is timezone aware (``DateTime(timezone=True)``) and is
  populated with ``datetime.now(UTC)`` so PostgreSQL and SQLite agree.
* Domain values that are enumerated (severity, status, ...) are declared as
  module level tuples and enforced with ``CheckConstraint`` so bad data can
  never reach the database, even from a direct SQL client.
* Child rows are always removed with their parent through
  ``cascade="all, delete-orphan"``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from extensions import db
from sqlalchemy import DateTime, text
from sqlalchemy.types import TypeDecorator

__all__ = [
    "db",
    "User",
    "Application",
    "Incident",
    "Deployment",
    "HealthCheck",
    "AuditLog",
    "Notification",
    "AuthToken",
    "APPLICATION_ENVIRONMENTS",
    "APPLICATION_STATUSES",
    "HEALTH_STATUSES",
    "INCIDENT_SEVERITIES",
    "INCIDENT_STATUSES",
    "DEPLOYMENT_STATUSES",
    "HTTP_METHODS",
    "severity_rank",
]

APPLICATION_ENVIRONMENTS = ("Development", "Staging", "Production")
APPLICATION_STATUSES = ("Operational", "Maintenance", "Decommissioned")
HEALTH_STATUSES = ("UP", "DOWN", "DEGRADED", "UNKNOWN")
INCIDENT_SEVERITIES = ("Low", "Medium", "High", "Critical")
INCIDENT_STATUSES = ("Open", "Investigating", "Resolved")
DEPLOYMENT_STATUSES = ("Successful", "Failed", "In Progress", "Rolled Back")
HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

_SEVERITY_RANK = {name: index for index, name in enumerate(INCIDENT_SEVERITIES)}


class UTCDateTime(TypeDecorator):
    """A timestamp column that is always timezone aware in Python.

    SQLite has no native timestamp type and silently drops the offset on the
    way back out, so a plain ``DateTime(timezone=True)`` returns naive
    datetimes on SQLite and aware ones on PostgreSQL. Comparing the two raises
    ``TypeError``. Storing naive UTC and re-attaching the offset on read gives
    one consistent type on every backend.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


TZDateTime = UTCDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


def severity_rank(severity: str) -> int:
    """Return a sortable rank for a severity label (Low < Critical)."""
    return _SEVERITY_RANK.get(severity, 0)


def _check(table: str, name: str, allowed: tuple[str, ...]) -> db.CheckConstraint:
    values = ", ".join(f"'{value}'" for value in allowed)
    return db.CheckConstraint(
        f"{name} IN ({values})",
        name=f"ck_{table}_{name}",
    )


class TimestampMixin:
    created_at = db.Column(
        TZDateTime,
        nullable=False,
        default=utcnow,
        index=True,
    )


class User(TimestampMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(100), nullable=False)

    email = db.Column(db.String(150), unique=True, nullable=False, index=True)

    password_hash = db.Column(db.String(255), nullable=False)

    role = db.Column(db.String(20), default="viewer", nullable=False)

    is_verified = db.Column(db.Boolean, default=False, nullable=False, index=True)

    last_login_at = db.Column(TZDateTime, nullable=True)

    failed_login_count = db.Column(db.Integer, default=0, nullable=False)

    locked_until = db.Column(TZDateTime, nullable=True)

    password_changed_at = db.Column(
        TZDateTime,
        default=utcnow,
        nullable=False,
    )

    auth_tokens = db.relationship(
        "AuthToken",
        back_populates="user",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # At most one Admin may exist. Registration decides "first account becomes
    # Admin" from a count read that a concurrent signup can also observe, so the
    # invariant is enforced by the database instead. The losing request catches
    # the IntegrityError and retries as a Viewer.
    __table_args__ = (
        db.CheckConstraint("length(role) > 0", name="ck_users_role_non_empty"),
        db.Index(
            "uq_users_single_admin",
            "role",
            unique=True,
            sqlite_where=text("role = 'Admin'"),
            postgresql_where=text("role = 'Admin'"),
        ),
    )


class Application(TimestampMixin, db.Model):
    __tablename__ = "applications"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(100), nullable=False, index=True)

    url = db.Column(db.String(500), nullable=False)

    environment = db.Column(
        db.String(50),
        default="Development",
        nullable=False,
        index=True,
    )

    status = db.Column(
        db.String(50),
        default="Operational",
        nullable=False,
        index=True,
    )

    description = db.Column(db.Text, nullable=True)

    # ---- Monitoring configuration -------------------------------------
    http_method = db.Column(db.String(10), default="GET", nullable=False)

    timeout_seconds = db.Column(db.Integer, default=5, nullable=False)

    check_interval_seconds = db.Column(db.Integer, default=60, nullable=False)

    expected_status_codes = db.Column(
        db.String(200),
        default="200-399",
        nullable=False,
        comment="Accepted HTTP status codes or inclusive ranges.",
    )

    expected_body_keyword = db.Column(db.String(200), nullable=True)

    request_headers = db.Column(db.Text, nullable=True)

    is_paused = db.Column(db.Boolean, default=False, nullable=False)

    # ---- Last known state ---------------------------------------------
    last_checked_at = db.Column(TZDateTime, nullable=True)

    last_health_status = db.Column(
        db.String(20),
        nullable=True,
        index=True,
    )

    last_response_time = db.Column(db.Float, nullable=True)

    last_http_status = db.Column(db.Integer, nullable=True)

    consecutive_failures = db.Column(db.Integer, default=0, nullable=False)

    uptime_percentage = db.Column(db.Float, default=100.0, nullable=False)

    incidents = db.relationship(
        "Incident",
        back_populates="application",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    deployments = db.relationship(
        "Deployment",
        back_populates="application",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    health_checks = db.relationship(
        "HealthCheck",
        back_populates="application",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        _check("applications", "environment", APPLICATION_ENVIRONMENTS),
        _check("applications", "status", APPLICATION_STATUSES),
        _check("applications", "http_method", HTTP_METHODS),
        db.CheckConstraint("timeout_seconds > 0", name="ck_applications_timeout_positive"),
        db.CheckConstraint("check_interval_seconds > 0", name="ck_applications_interval_positive"),
    )

    # ------------------------------------------------------------------
    @property
    def accepted_status_codes(self) -> set[int]:
        """Expand ``expected_status_codes`` (``200-399,404``) into a set."""
        return expand_status_codes(self.expected_status_codes)

    @property
    def parsed_request_headers(self) -> dict[str, str]:
        if not self.request_headers:
            return {}
        try:
            loaded = json.loads(self.request_headers)
        except (TypeError, ValueError):
            return {}
        if not isinstance(loaded, dict):
            return {}
        return {str(key): str(value) for key, value in loaded.items()}

    def monitoring_summary(self) -> dict:
        return {
            "http_method": self.http_method,
            "timeout_seconds": self.timeout_seconds,
            "check_interval_seconds": self.check_interval_seconds,
            "expected_status_codes": self.expected_status_codes,
            "expected_body_keyword": self.expected_body_keyword,
            # The edit form reads request_headers from here. Leaving it out
            # means the field renders empty when an application already has
            # custom headers, and the next save silently drops them.
            "request_headers": self.request_headers,
            "is_paused": self.is_paused,
        }


def expand_status_codes(spec: str | None) -> set[int]:
    """Expand a status code specification into a concrete set of integers.

    Supports single codes (``404``) and inclusive ranges (``200-399``).
    An empty or invalid specification falls back to ``200-399``.
    """
    default = set(range(200, 400))
    if not spec:
        return default

    expanded: set[int] = set()
    for chunk in spec.split(","):
        token = chunk.strip()
        if not token:
            continue
        if "-" in token:
            start_raw, _, end_raw = token.partition("-")
            try:
                start, end = int(start_raw), int(end_raw)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            expanded.update(range(start, min(end, 599) + 1))
        else:
            try:
                expanded.add(int(token))
            except ValueError:
                continue

    return expanded or default


class HealthCheck(db.Model):
    __tablename__ = "health_checks"

    id = db.Column(db.Integer, primary_key=True)

    application_id = db.Column(
        db.Integer,
        db.ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
    )

    status = db.Column(db.String(20), nullable=False, index=True)

    http_status = db.Column(db.Integer, nullable=True)

    response_time = db.Column(db.Float, nullable=True)

    checked_at = db.Column(
        TZDateTime,
        default=utcnow,
        nullable=False,
        index=True,
    )

    error_message = db.Column(db.Text, nullable=True)

    triggered_incident_id = db.Column(
        db.Integer,
        db.ForeignKey("incidents.id", ondelete="SET NULL"),
        nullable=True,
    )

    application = db.relationship("Application", back_populates="health_checks")

    __table_args__ = (
        _check("health_checks", "status", HEALTH_STATUSES),
        db.Index("ix_health_checks_application_checked", "application_id", "checked_at"),
    )


class Incident(TimestampMixin, db.Model):
    __tablename__ = "incidents"

    id = db.Column(db.Integer, primary_key=True)

    application_id = db.Column(
        db.Integer,
        db.ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title = db.Column(db.String(200), nullable=False)

    severity = db.Column(
        db.String(50),
        default="Medium",
        nullable=False,
        index=True,
    )

    status = db.Column(
        db.String(50),
        default="Open",
        nullable=False,
        index=True,
    )

    description = db.Column(db.Text, nullable=True)

    root_cause = db.Column(db.Text, nullable=True)

    assignee = db.Column(db.String(120), nullable=True)

    source = db.Column(
        db.String(30),
        default="manual",
        nullable=False,
        comment="manual or monitoring",
    )

    detected_at = db.Column(TZDateTime, default=utcnow, nullable=False)

    acknowledged_at = db.Column(TZDateTime, nullable=True)

    resolved_at = db.Column(TZDateTime, nullable=True, index=True)

    application = db.relationship("Application", back_populates="incidents")

    __table_args__ = (
        _check("incidents", "severity", INCIDENT_SEVERITIES),
        _check("incidents", "status", INCIDENT_STATUSES),
        _check("incidents", "source", ("manual", "monitoring")),
        db.Index("ix_incidents_application_status", "application_id", "status"),
    )

    @property
    def is_open(self) -> bool:
        return self.status != "Resolved"

    @property
    def detection_to_resolution_minutes(self) -> float | None:
        if not self.resolved_at or not self.detected_at:
            return None
        return round(
            (self.resolved_at - self.detected_at).total_seconds() / 60,
            2,
        )


class Deployment(TimestampMixin, db.Model):
    __tablename__ = "deployments"

    id = db.Column(db.Integer, primary_key=True)

    application_id = db.Column(
        db.Integer,
        db.ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version = db.Column(db.String(100), nullable=False)

    environment = db.Column(
        db.String(50),
        default="Development",
        nullable=False,
        index=True,
    )

    status = db.Column(
        db.String(50),
        default="Successful",
        nullable=False,
        index=True,
    )

    commit_hash = db.Column(db.String(64), nullable=True)

    changelog = db.Column(db.Text, nullable=True)

    duration_seconds = db.Column(db.Integer, nullable=True)

    deployed_by = db.Column(db.String(120), nullable=True)

    deployed_at = db.Column(
        TZDateTime,
        default=utcnow,
        nullable=False,
        index=True,
    )

    rolled_back_at = db.Column(TZDateTime, nullable=True)

    application = db.relationship("Application", back_populates="deployments")

    __table_args__ = (
        _check("deployments", "status", DEPLOYMENT_STATUSES),
        _check("applications", "environment", APPLICATION_ENVIRONMENTS),
    )


class AuditLog(db.Model):
    """Append only record of every meaningful state change.

    Doubles as the activity timeline rendered on the dashboard and as the
    security audit trail for the incident detail view.
    """

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)

    actor_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    actor_name = db.Column(db.String(120), nullable=True)

    action = db.Column(db.String(80), nullable=False, index=True)

    entity_type = db.Column(db.String(40), nullable=True, index=True)

    entity_id = db.Column(db.Integer, nullable=True, index=True)

    summary = db.Column(db.String(500), nullable=True)

    detail = db.Column(db.Text, nullable=True)

    ip_address = db.Column(db.String(64), nullable=True)

    created_at = db.Column(
        TZDateTime,
        default=utcnow,
        nullable=False,
        index=True,
    )

    __table_args__ = (
        db.Index("ix_audit_entity", "entity_type", "entity_id"),
    )


class Notification(db.Model):
    """Outbox row describing one delivery attempt of an alert."""

    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)

    incident_id = db.Column(
        db.Integer,
        db.ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    application_id = db.Column(
        db.Integer,
        db.ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    channel = db.Column(db.String(30), nullable=False, index=True)

    target = db.Column(db.String(300), nullable=True)

    event = db.Column(db.String(60), nullable=False, index=True)

    status = db.Column(
        db.String(20),
        default="pending",
        nullable=False,
        index=True,
    )

    attempts = db.Column(db.Integer, default=0, nullable=False)

    last_error = db.Column(db.Text, nullable=True)

    sent_at = db.Column(TZDateTime, nullable=True)

    created_at = db.Column(
        TZDateTime,
        default=utcnow,
        nullable=False,
        index=True,
    )

    __table_args__ = (
        # "superseded" marks a row that was replaced by a later delivery attempt, so a
        # retry does not leave it counting as a permanent failure.
        _check(
            "notifications",
            "status",
            ("pending", "sent", "failed", "skipped", "superseded"),
        ),
        _check("notifications", "channel", ("webhook", "email", "log")),
    )


class AuthToken(db.Model):
    """Single use, hashed token backing password reset and email verification.

    Only the SHA-256 digest of the token is stored, so a database leak cannot be
    replayed against the application. Expiry is enforced on read rather than by
    a database constraint so a token stays queryable for reporting purposes.
    """

    __tablename__ = "auth_tokens"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    purpose = db.Column(db.String(30), nullable=False, index=True)

    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)

    expires_at = db.Column(TZDateTime, nullable=False, index=True)

    used_at = db.Column(TZDateTime, nullable=True)

    created_at = db.Column(
        TZDateTime,
        default=utcnow,
        nullable=False,
        index=True,
    )

    requested_ip = db.Column(db.String(64), nullable=True)

    user = db.relationship("User", back_populates="auth_tokens")

    __table_args__ = (
        _check("auth_tokens", "purpose", ("password_reset", "email_verification")),
        db.Index("ix_auth_tokens_user_purpose", "user_id", "purpose"),
    )

    @property
    def is_usable(self) -> bool:
        from datetime import UTC as _UTC

        if self.used_at is not None:
            return False
        expires_at = self.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=_UTC)
        return expires_at is None or expires_at > datetime.now(_UTC)
