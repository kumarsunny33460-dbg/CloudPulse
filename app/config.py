"""Application configuration objects for CloudPulse.

Configuration is resolved from environment variables so the same image can be
promoted across local development, CI, and production without code changes.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger("cloudpulse.config")

BASE_DIR = Path(__file__).resolve().parent

# Where the SQLite fallback lives. Defaults to ``<repo>/instance`` for a local
# checkout, where the modules sit in ``<repo>/app``. In the container the
# application is mounted at the image root, so the same calculation yields ``/``
# and would try to create ``/instance``. INSTANCE_DIR is therefore
# configurable, and the image sets it explicitly.
INSTANCE_DIR_ENV = "INSTANCE_DIR"


def default_instance_dir() -> Path:
    """Directory for the SQLite fallback, honouring ``INSTANCE_DIR``."""
    configured = env_str(INSTANCE_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    return BASE_DIR.parent / "instance"

TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}


def env_str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in TRUTHY:
        return True
    if raw in FALSY:
        return False
    return default


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def normalize_database_url(url: str) -> str:
    """Normalize provider specific database URLs.

    Render, Heroku and some managed Postgres providers still emit the legacy
    ``postgres://`` scheme which SQLAlchemy refuses to load.
    """
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql+psycopg2://"):
        return url
    return url


def resolve_database_uri(instance_dir: Path | None = None) -> str:
    configured = env_str("DATABASE_URL")
    if configured:
        return normalize_database_url(configured)

    # Resolved per call rather than at import: INSTANCE_DIR is read from the
    # environment, which load_dotenv may only populate later.
    directory = instance_dir or default_instance_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + (directory / "cloudpulse.db").as_posix()


def resolve_secret_key() -> str:
    """Return the signing key, generating an ephemeral one when absent.

    Outside production a missing ``SECRET_KEY`` falls back to a random value so
    a developer never runs with a key that is published in the repository. The
    fallback is ephemeral on purpose: sessions do not survive a restart, which
    is the correct trade-off for a local run. ``ProductionConfig`` refuses to
    boot without an explicit key instead.
    """
    configured = env_str("SECRET_KEY")
    if configured:
        return configured

    if env_bool("ALLOW_INSECURE_SECRET_KEY", False):
        return "cloudpulse-insecure-development-key"

    logger.warning(
        "SECRET_KEY is not set; generating an ephemeral development key. "
        "Sessions will not survive a restart and must not be relied on. Set "
        "SECRET_KEY (or ALLOW_INSECURE_SECRET_KEY=true) to pin one."
    )
    return secrets.token_urlsafe(48)


class BaseConfig:
    """Settings shared by every environment.

    ``SECRET_KEY`` and ``SQLALCHEMY_DATABASE_URI`` are placeholders. They are
    deliberately not resolved here: a class body runs at import time, which is
    before ``load_dotenv()`` populates ``os.environ``, so an import-time value
    would ignore anything supplied in ``.env``. ``apply_dynamic_config`` fills
    them in once the app is built.
    """

    #: Overwritten by :func:`apply_dynamic_config`.
    SECRET_KEY = None

    #: Overwritten by :func:`apply_dynamic_config`.
    SQLALCHEMY_DATABASE_URI = None

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": env_int("DB_POOL_RECYCLE", 280),
    }

    JSON_SORT_KEYS = False
    MAX_CONTENT_LENGTH = env_int("MAX_CONTENT_LENGTH", 2 * 1024 * 1024)

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", False)
    PERMANENT_SESSION_LIFETIME = env_int("SESSION_LIFETIME_SECONDS", 60 * 60 * 8)
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------
    HEALTH_CHECK_TIMEOUT = env_int("HEALTH_CHECK_TIMEOUT", 5)
    HEALTH_CHECK_MAX_RESPONSE_BYTES = env_int("HEALTH_CHECK_MAX_RESPONSE_BYTES", 65536)
    HEALTH_CHECK_USER_AGENT = env_str("HEALTH_CHECK_USER_AGENT", "CloudPulse-Monitor/1.0")
    HEALTH_CHECK_ALLOWED_SCHEMES = tuple(
        env_list("HEALTH_CHECK_ALLOWED_SCHEMES", "http,https")
    )
    # Verify TLS certificates. Set to false only when CloudPulse runs behind a
    # corporate TLS-intercepting proxy whose root CA is not in the trust store.
    HEALTH_CHECK_VERIFY_TLS = env_bool("HEALTH_CHECK_VERIFY_TLS", True)
    HEALTH_CHECK_RETENTION_DAYS = env_int("HEALTH_CHECK_RETENTION_DAYS", 30)
    # Number of recent health checks that make up the reported uptime figure
    UPTIME_WINDOW_CHECKS = env_int("UPTIME_WINDOW_CHECKS", 200)
    MONITOR_INTERVAL_SECONDS = env_int("MONITOR_INTERVAL_SECONDS", 60)
    MONITOR_DEGRADED_AFTER_CHECKS = env_int("MONITOR_DEGRADED_AFTER_CHECKS", 2)
    AUTO_RESOLVE_INCIDENTS = env_bool("AUTO_RESOLVE_INCIDENTS", True)
    AUTO_CREATE_INCIDENTS = env_bool("AUTO_CREATE_INCIDENTS", True)

    # ------------------------------------------------------------------
    # Incident management
    # ------------------------------------------------------------------
    SLA_MINUTES = {
        "Critical": env_int("SLA_CRITICAL_MINUTES", 30),
        "High": env_int("SLA_HIGH_MINUTES", 120),
        "Medium": env_int("SLA_MEDIUM_MINUTES", 480),
        "Low": env_int("SLA_LOW_MINUTES", 1440),
    }

    # ------------------------------------------------------------------
    # Background scheduler
    # ------------------------------------------------------------------
    ENABLE_SCHEDULER = env_bool("ENABLE_SCHEDULER", False)
    SCHEDULER_TIMEZONE = env_str("SCHEDULER_TIMEZONE", "UTC")

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    NOTIFY_WEBHOOK_URL = env_str("NOTIFY_WEBHOOK_URL")
    NOTIFY_WEBHOOK_SECRET = env_str("NOTIFY_WEBHOOK_SECRET")
    NOTIFY_SMTP_HOST = env_str("NOTIFY_SMTP_HOST")
    NOTIFY_SMTP_PORT = env_int("NOTIFY_SMTP_PORT", 587)
    NOTIFY_SMTP_USER = env_str("NOTIFY_SMTP_USER")
    NOTIFY_SMTP_PASSWORD = env_str("NOTIFY_SMTP_PASSWORD")
    NOTIFY_SMTP_FROM = env_str("NOTIFY_SMTP_FROM", "cloudpulse@localhost")
    NOTIFY_SMTP_TO = env_list("NOTIFY_SMTP_TO")
    NOTIFY_SMTP_USE_TLS = env_bool("NOTIFY_SMTP_USE_TLS", True)
    NOTIFY_ON_INCIDENT_OPEN = env_bool("NOTIFY_ON_INCIDENT_OPEN", True)
    NOTIFY_ON_INCIDENT_RESOLVED = env_bool("NOTIFY_ON_INCIDENT_RESOLVED", True)
    NOTIFY_ON_DEPLOYMENT = env_bool("NOTIFY_ON_DEPLOYMENT", True)
    # Absolute base URL used in outgoing links. Behind a proxy or load balancer
    # the inbound request host is often the internal name, so this should be set
    # to the public address of the deployment.
    PUBLIC_BASE_URL = env_str("PUBLIC_BASE_URL")

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------
    DEFAULT_PAGE_SIZE = env_int("DEFAULT_PAGE_SIZE", 25)
    MAX_PAGE_SIZE = env_int("MAX_PAGE_SIZE", 200)

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------
    CSRF_PROTECTION_ENABLED = env_bool("CSRF_PROTECTION_ENABLED", True)
    CSRF_TOKEN_TTL_SECONDS = env_int("CSRF_TOKEN_TTL_SECONDS", 3600)
    STRICT_ORIGIN_CHECK = env_bool("STRICT_ORIGIN_CHECK", True)
    ENABLE_DEBUG_ROUTE = env_bool("ENABLE_DEBUG_ROUTE", False)

    # Comma separated addresses or CIDR ranges whose X-Forwarded-For header may
    # be believed. Leave empty when the app is reached directly: trusting the
    # header unconditionally lets any client claim a fresh IP on every request,
    # which defeats the login rate limiter and misleads the audit trail.
    TRUSTED_PROXY_NETWORKS = env_str("TRUSTED_PROXY_NETWORKS")

    # Brute force protection. Attempts are tracked per (identifier, client IP)
    # pair so one attacker cannot lock every user out of the application.
    LOGIN_MAX_ATTEMPTS = env_int("LOGIN_MAX_ATTEMPTS", 5)
    LOGIN_ATTEMPT_WINDOW_SECONDS = env_int("LOGIN_ATTEMPT_WINDOW_SECONDS", 300)
    LOGIN_LOCKOUT_SECONDS = env_int("LOGIN_LOCKOUT_SECONDS", 900)
    # Failed registration attempts for an email that is not yet a user.
    REGISTRATION_MAX_ATTEMPTS = env_int("REGISTRATION_MAX_ATTEMPTS", 5)
    # Self service account recovery.
    PASSWORD_RESET_TOKEN_TTL_SECONDS = env_int("PASSWORD_RESET_TOKEN_TTL_SECONDS", 3600)
    PASSWORD_RESET_MAX_ATTEMPTS = env_int("PASSWORD_RESET_MAX_ATTEMPTS", 5)
    REQUIRE_EMAIL_VERIFICATION = env_bool("REQUIRE_EMAIL_VERIFICATION", False)
    EMAIL_VERIFICATION_TOKEN_TTL_SECONDS = env_int(
        "EMAIL_VERIFICATION_TOKEN_TTL_SECONDS", 86400
    )
    MIN_PASSWORD_LENGTH = env_int("MIN_PASSWORD_LENGTH", 8)

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------
    AUDIT_LOG_RETENTION_DAYS = env_int("AUDIT_LOG_RETENTION_DAYS", 180)
    NOTIFICATION_RETENTION_DAYS = env_int("NOTIFICATION_RETENTION_DAYS", 30)
    # How far back the manual retry endpoint will reach. Without a bound, a
    # single retained outbox row can fire a webhook years after the fact.
    # 0 disables the bound.
    NOTIFICATION_RETRY_MAX_AGE_DAYS = env_int("NOTIFICATION_RETRY_MAX_AGE_DAYS", 7)
    RETENTION_JOB_HOURS = env_int("RETENTION_JOB_HOURS", 6)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    # ``json`` emits one structured object per line for Loki/ELK ingestion,
    # ``text`` keeps the human readable pipe separated format used locally.
    LOG_FORMAT = (env_str("LOG_FORMAT", "text") or "text").lower()
    LOG_LEVEL = (env_str("LOG_LEVEL", "") or "").upper()
    LOG_INCLUDE_REQUEST_ID = env_bool("LOG_INCLUDE_REQUEST_ID", True)
    LOG_REQUEST_BODY = env_bool("LOG_REQUEST_BODY", False)
    # The dashboard loads only first-party assets, so a strict policy works.
    # Off by default because the inline style block in the auth templates would
    # need a hash or nonce under style-src 'self'.
    CSP_ENABLED = env_bool("CSP_ENABLED", False)

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------
    AUTO_CREATE_SCHEMA = env_bool("AUTO_CREATE_SCHEMA", True)
    # Adds missing columns and indexes to an already existing database so an
    # upgraded application keeps working against a pre-existing SQLite file.
    AUTO_SCHEMA_SYNC = env_bool("AUTO_SCHEMA_SYNC", True)


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    AUTO_CREATE_SCHEMA = True


class TestingConfig(BaseConfig):
    TESTING = True
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS: dict = {}
    AUTO_CREATE_SCHEMA = True
    # The test fixture builds the schema with create_all() before yielding, so
    # the boot-time column and index sync has nothing left to do. Leaving it on
    # made every one of the ~300 app constructions repeat that work.
    AUTO_SCHEMA_SYNC = False
    ENABLE_SCHEDULER = False
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    CSRF_PROTECTION_ENABLED = False
    STRICT_ORIGIN_CHECK = False
    SECRET_KEY = "cloudpulse-testing-secret-key"
    # Rate limiting and account recovery are disabled so the suite is not
    # order dependent; dedicated tests re-enable them explicitly.
    LOGIN_MAX_ATTEMPTS = 0
    REGISTRATION_MAX_ATTEMPTS = 0
    PASSWORD_RESET_MAX_ATTEMPTS = 0
    REQUIRE_EMAIL_VERIFICATION = False
    LOG_FORMAT = "text"


class ProductionConfig(BaseConfig):
    DEBUG = False
    AUTO_CREATE_SCHEMA = env_bool("AUTO_CREATE_SCHEMA", False)
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", True)


#: Values that appear in the repository -- the Helm chart defaults, the
#: kustomize overlays and the runbook -- and are therefore public. Accepting
#: any of them in production would mean the signing key is a value anyone can
#: read in this repository, which is worse than having no key at all because
#: it looks configured.
INSECURE_PLACEHOLDER_KEYS = frozenset(
    {
        "replace-me-with-a-long-random-value",
        "replace-me",
        "changeme",
        "change-me",
        "your-secret-key-here",
        "cloudpulse-development-secret-key",
        "cloudpulse-insecure-development-key",
        "secret",
        "insecure",
    }
)


def _looks_like_placeholder_key(value: str | None) -> bool:
    """True when the key is a published placeholder rather than a real secret."""
    if not value:
        return False
    lowered = value.strip().lower()
    if lowered in INSECURE_PLACEHOLDER_KEYS:
        return True
    # Anything containing the obvious marker words is treated as a placeholder
    # too, since operators rename them freely.
    return any(marker in lowered for marker in ("replace-me", "changeme", "your-secret"))


def secret_key_problem() -> str | None:
    """Describe why the signing key is unusable, or ``None`` when it is fine.

    Split out from :func:`validate_config` so the same rules can drive both the
    hard stop and the diagnostic mode. A hosted platform reports a crashed
    worker as a bare 503, which tells the operator nothing; naming the exact
    variable in the page they already have open is the difference between a
    five minute fix and an evening.
    """
    key = (env_str("SECRET_KEY") or "").strip()

    if not key:
        return (
            "SECRET_KEY is not set. In Render: open your service, go to the "
            "Environment tab, click Add, set the key to SECRET_KEY, press "
            "Generate for the value, then Save and hit Manual Deploy."
        )

    if _looks_like_placeholder_key(key):
        return (
            f"SECRET_KEY is set to {key!r}, which is a value published in this "
            "repository. Anyone who can read it can forge a session cookie and "
            "sign in as any user. Replace it with a generated value."
        )

    if len(key) < 32:
        return (
            f"SECRET_KEY is only {len(key)} characters; the minimum is 32. "
            "Press Generate in the Render dashboard rather than typing one."
        )

    return None


def validate_config(name: str, settings) -> None:
    """Fail fast on a configuration that would be unsafe in production.

    ``app.config.from_object`` never instantiates a config class, so the check
    cannot live in a constructor; ``create_app`` calls this instead.

    When ``CLOUDPULSE_DIAGNOSTIC_MODE`` is on, a bad key does not raise. The
    application starts with an ephemeral key that cannot sign a usable session
    and serves a page explaining the fix. Nothing is reachable behind it, so this
    trades nothing away in exchange for a deploy that is self-explanatory.
    """
    if name != "production":
        return

    if env_bool("ALLOW_INSECURE_SECRET_KEY", False):
        return

    problem = secret_key_problem()
    if problem is None:
        _warn_about_production_posture(settings)
        return

    if env_bool("CLOUDPULSE_DIAGNOSTIC_MODE", True):
        logger.error("Starting in diagnostic mode: %s", problem)
        return

    raise RuntimeError(
        "SECRET_KEY is unusable in production. Generate one with: "
        'python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )


def _warn_about_production_posture(settings) -> None:
    """Log the production settings that are weaker than they should be."""
    if not settings.get("SESSION_COOKIE_SECURE"):
        logger.warning(
            "SESSION_COOKIE_SECURE is disabled in production; session cookies "
            "will be sent over plain HTTP."
        )

    if settings.get("STRICT_ORIGIN_CHECK") is False:
        logger.warning(
            "STRICT_ORIGIN_CHECK is disabled in production; cross-origin state "
            "changes are no longer rejected."
        )


CONFIGURATIONS = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}

#: Keys whose value can only be decided once the process environment is fully
#: populated. They are resolved by :func:`apply_dynamic_config` instead of in
#: the class body.
#:
#: Evaluating them at import time froze them before ``load_dotenv()`` ran, so a
#: ``SECRET_KEY`` supplied in ``.env`` was ignored and production ran on a
#: throwaway key while the guard that should have refused it passed.
DYNAMIC_KEYS = ("SECRET_KEY", "SQLALCHEMY_DATABASE_URI")


def apply_dynamic_config(app_config: dict) -> None:
    """Re-resolve import-time settings against the current environment.

    ``app.config.from_object`` reads class attributes, which were evaluated when
    this module was first imported -- before ``load_dotenv()`` had a chance to
    populate ``os.environ``. Recomputing here guarantees the values actually in
    use match what the operator supplied.

    A value the selected config class set explicitly is left alone. Only the
    ``None`` placeholders in :class:`BaseConfig` are filled in. Overwriting
    unconditionally would clobber ``TestingConfig``'s in-memory
    ``sqlite://`` with the development database file, so the test suite would
    run its ``drop_all()`` against the developer's real data.
    """
    if not app_config.get("SECRET_KEY"):
        app_config["SECRET_KEY"] = resolve_secret_key()

    if not app_config.get("SQLALCHEMY_DATABASE_URI"):
        app_config["SQLALCHEMY_DATABASE_URI"] = resolve_database_uri()


def resolve_config_name(name: str | None = None) -> str:
    if name:
        return name.lower()

    env_name = env_str("APP_ENV") or env_str("FLASK_ENV")
    if env_name:
        return env_name.lower()

    if env_bool("RENDER", False) or env_str("RENDER_SERVICE_NAME"):
        return "production"

    return "development"


def get_config(name: str | None = None):
    """Return the config class for an environment name.

    An unrecognised name raises instead of falling back to ``DevelopmentConfig``.
    Silently defaulting meant a typo such as ``APP_ENV=prodction`` started the
    application with ``DEBUG=True`` and the Werkzeug debugger exposed.
    """
    key = resolve_config_name(name)
    if key not in CONFIGURATIONS:
        raise RuntimeError(
            f"Unknown APP_ENV {key!r}. Expected one of: "
            f"{', '.join(sorted(CONFIGURATIONS))}. "
            "An unknown name is refused rather than defaulting to development, "
            "because a typo would otherwise expose the debugger in production."
        )
    return CONFIGURATIONS[key]
