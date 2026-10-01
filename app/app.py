"""CloudPulse application entrypoint.

The module exposes a ready to serve ``app`` object for gunicorn
(``gunicorn app:app``) and a ``create_app`` factory used by the test suite.

Layout
------
``config``      environment driven configuration objects
``models``      SQLAlchemy models
``serializers`` JSON payload builders
``services``    domain logic (health checks, incidents, notifications, metrics)
``api``         REST blueprints
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

# The ``app`` directory is the import root for this project, matching the
# ``gunicorn app:app`` / ``python app.py`` entrypoints.
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import api  # noqa: E402
from config import (  # noqa: E402
    apply_dynamic_config,
    get_config,
    resolve_config_name,
    validate_config,
)
from extensions import db  # noqa: E402
from flask import (  # noqa: E402
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from models import User  # noqa: E402
from security import admin_required, login_required  # noqa: E402,F401
from serializers import user_to_dict  # noqa: E402
from services import accounts, logging_config, metrics, ratelimit, scheduler  # noqa: E402
from services import schema as schema_service  # noqa: E402

# Backwards compatible aliases for the original monolith helpers.
from services.incidents import create_incident_if_needed  # noqa: E402,F401
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

try:  # Optional: loads .env during local development only.
    from dotenv import load_dotenv

    load_dotenv(APP_DIR.parent / ".env")
except ImportError:  # pragma: no cover - optional dependency
    pass


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

logger = logging.getLogger("cloudpulse")


def create_app(config_name: str | None = None) -> Flask:
    # Resolve the environment name once and use that single value everywhere.
    # Deriving ENV_NAME separately from `get_config` is what allowed a Render
    # deployment to select ProductionConfig while recording ENV_NAME as
    # "development", which made validate_config() return early and skip the
    # SECRET_KEY guard entirely.
    env_name = resolve_config_name(config_name)
    configuration = get_config(env_name)

    flask_app = Flask(__name__, instance_relative_config=False)
    flask_app.config.from_object(configuration)

    # SECRET_KEY and the database URI are re-resolved now that load_dotenv has
    # run, so a value supplied in .env is actually the one in use.
    apply_dynamic_config(flask_app.config)

    flask_app.config["ENV_NAME"] = env_name
    flask_app.config["GIT_COMMIT"] = os.getenv("GIT_COMMIT_SHA", "unknown")

    validate_config(flask_app.config["ENV_NAME"], flask_app.config)

    # In development Jinja caches compiled templates and Flask caches static
    # files, which makes a freshly edited page look unchanged in the browser.
    # Both are disabled outside production so what you edit is what you see.
    if flask_app.config.get("DEBUG") or flask_app.config.get("TESTING"):
        flask_app.config["TEMPLATES_AUTO_RELOAD"] = True
        flask_app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    _configure_logging(flask_app)
    _configure_csrf(flask_app)
    _configure_database(flask_app)
    _configure_sessions(flask_app)
    _register_lifecycle(flask_app)
    _register_error_handlers(flask_app)
    _register_auth_routes(flask_app)
    api.register(flask_app)

    logger.info(
        "CloudPulse initialised | Environment: %s | Database: %s | Log format: %s",
        flask_app.config["ENV_NAME"],
        _database_label(flask_app),
        flask_app.config.get("LOG_FORMAT", "text"),
    )

    return flask_app


# ----------------------------------------------------------------------
# Configuration helpers
# ----------------------------------------------------------------------

def _configure_logging(flask_app: Flask) -> None:
    logging_config.configure(flask_app)


def _configure_csrf(flask_app: Flask) -> None:
    """Enable Flask-WTF CSRF protection for the browser facing form routes.

    The JSON API is protected differently: it requires the
    ``X-Requested-With: XMLHttpRequest`` header, which a cross-origin page cannot
    set without a CORS pre-flight that this application never approves. Applying
    a form token to ``fetch`` calls would add a round trip for no additional
    protection, so the API blueprint is exempted and the origin guard below does
    the work.

    When protection is disabled the ``csrf_token`` template global still exists
    and renders empty, so the templates need no conditional and never break.
    """
    if not flask_app.config.get("CSRF_PROTECTION_ENABLED"):
        flask_app.jinja_env.globals.setdefault("csrf_token", lambda: "")
        return

    try:
        from flask_wtf.csrf import CSRFProtect
    except ImportError:  # pragma: no cover - optional dependency
        logger.warning(
            "Flask-WTF is not installed; CSRF protection is disabled. Install "
            "it with: pip install Flask-WTF"
        )
        flask_app.jinja_env.globals.setdefault("csrf_token", lambda: "")
        return

    csrf = CSRFProtect(flask_app)
    for blueprint in api.csrf_exemptions():
        csrf.exempt(blueprint)
    flask_app.extensions["csrf"] = csrf


def _configure_database(flask_app: Flask) -> None:
    db.init_app(flask_app)

    with flask_app.app_context():
        import models  # noqa: F401  (ensures the metadata is fully loaded)

        if flask_app.config.get("AUTO_CREATE_SCHEMA"):
            if flask_app.config.get("AUTO_SCHEMA_SYNC"):
                schema_service.sync_schema()
            else:
                db.create_all()

    try:  # Optional migration support; harmless when unavailable.
        from flask_migrate import Migrate

        Migrate(flask_app, db, directory=str(APP_DIR.parent / "migrations"))
    except ImportError:  # pragma: no cover - optional dependency
        logger.info("Flask-Migrate is not installed, migrations are unavailable")


def _configure_sessions(flask_app: Flask) -> None:
    from datetime import timedelta

    flask_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        seconds=flask_app.config["PERMANENT_SESSION_LIFETIME"]
    )
    flask_app.config["SESSION_COOKIE_NAME"] = os.getenv("SESSION_COOKIE_NAME", "cloudpulse_session")


def _database_label(flask_app: Flask) -> str:
    """Return the database target without logging any credentials."""
    uri = flask_app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if uri.startswith("sqlite"):
        return "sqlite"
    if "@" in uri:
        return uri.rsplit("@", 1)[-1]
    return uri or "unknown"


# ----------------------------------------------------------------------
# Request lifecycle
# ----------------------------------------------------------------------

def _register_lifecycle(flask_app: Flask) -> None:
    from flask import g

    @flask_app.before_request
    def load_identity():
        g.user_id = session.get("user_id")
        g.username = session.get("username")
        g.role = session.get("role")
        g.request_started_at = time.perf_counter()
        # Honour an upstream supplied id so a gateway correlation id survives;
        # otherwise mint one for this request.
        g.request_id = (
            request.headers.get("X-Request-ID") or logging_config.new_request_id()
        )

    @flask_app.before_request
    def guard_api_mutations():
        if not flask_app.config.get("CSRF_PROTECTION_ENABLED"):
            return None
        if request.method in SAFE_METHODS:
            return None
        if not request.path.startswith("/api/"):
            return None
        if not flask_app.config.get("STRICT_ORIGIN_CHECK"):
            return None

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return None

        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc == request.host:
            return None

        referer = request.headers.get("Referer")
        if referer and urlparse(referer).netloc == request.host:
            return None

        logger.warning("Blocked cross-origin state change | Path: %s", request.path)
        return jsonify({
            "error": "Cross-origin request blocked",
            "hint": "Send the X-Requested-With: XMLHttpRequest header with API calls.",
        }), 403

    @flask_app.after_request
    def finalize(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-XSS-Protection", "0")

        request_id = getattr(g, "request_id", None)
        if request_id:
            response.headers.setdefault("X-Request-ID", request_id)

        if flask_app.config.get("CSP_ENABLED", False):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            )

        if request.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"

        started = getattr(g, "request_started_at", None)
        if started is not None:
            try:
                metrics.record_request(
                    request.url_rule.rule if request.url_rule else request.path,
                    request.method,
                    response.status_code,
                    time.perf_counter() - started,
                )
            except Exception:  # pragma: no cover - never break a response
                pass

        return response

    @flask_app.teardown_appcontext
    def cleanup(exception=None):
        if exception is not None:
            try:
                db.session.rollback()
            except Exception:  # pragma: no cover - defensive
                pass


def _register_error_handlers(flask_app: Flask) -> None:
    from werkzeug.exceptions import HTTPException

    def wants_json() -> bool:
        return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"

    @flask_app.errorhandler(HTTPException)
    def handle_http_exception(error: HTTPException):
        status = error.code or 500
        if wants_json():
            return jsonify({"error": error.description or error.name}), status
        return error

    @flask_app.errorhandler(Exception)
    def handle_unexpected(error: Exception):
        from flask import current_app

        db.session.rollback()
        logger.exception("Unhandled application error | Path: %s", request.path)

        message = "Internal server error"
        if current_app.config.get("DEBUG"):
            message = f"{type(error).__name__}: {error}"

        if wants_json():
            return jsonify({"error": message}), 500

        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<title>CloudPulse - Error</title></head><body>"
            "<main style='font-family:system-ui;padding:48px'>"
            "<h1>Something went wrong</h1>"
            "<p>The request could not be completed. Please try again.</p>"
            "</main></body></html>",
            500,
        )


# ----------------------------------------------------------------------
# Authentication routes
#
# Authentication is intentionally left exactly as it was. Only the
# authorisation helpers above and the new REST layer were added around it.
# ----------------------------------------------------------------------

def _register_auth_routes(flask_app: Flask) -> None:
    @flask_app.route("/register", methods=["GET", "POST"])
    def register():
        if "user_id" in session:
            return redirect(url_for("home"))

        if request.method == "GET":
            return render_template("register.html")

        data = request.form
        username = data.get("username", "").strip()
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")

        if not username or not email or not password:
            return render_template(
                "register.html",
                error="All fields are required."
            )

        client_ip = _client_ip()

        # Reserve the attempt up front so parallel submissions cannot all pass
        # the check before any of them is recorded.
        if not ratelimit.reserve("register", email, client_ip):
            retry_after = ratelimit.retry_after_seconds("register", email, client_ip)
            logger.warning(
                "Registration throttled | Email: %s | IP: %s", email, client_ip
            )
            return render_template(
                "register.html",
                error=(
                    "Too many attempts from this device. "
                    f"Try again in {retry_after} seconds."
                ),
            ), 429

        strength_errors = accounts.password_strength_errors(password)
        if strength_errors:
            return render_template(
                "register.html",
                error=" ".join(strength_errors),
            )

        # A reservation is already recorded for this attempt, so a conflicting
        # registration needs no extra counting.
        existing_username = User.query.filter_by(username=username).first()
        if existing_username:
            return render_template(
                "register.html",
                error="Username already exists."
            )

        existing_email = User.query.filter_by(email=email).first()
        if existing_email:
            return render_template(
                "register.html",
                error="Email is already registered."
            )

        role = _first_user_role()
        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            role=role
        )

        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            # Two concurrent signups cannot both be the first user. The unique
            # index on users.role keeps exactly one Admin, so whichever loses
            # retries as a Viewer instead of silently creating a second admin.
            db.session.rollback()
            role = "Viewer"
            user = User(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                role=role,
            )
            db.session.add(user)
            db.session.commit()

        ratelimit.clear("register", email, client_ip)

        _send_welcome_email(user)

        return redirect(url_for("login"))

    @flask_app.route("/login", methods=["GET", "POST"])
    def login():
        if "user_id" in session:
            return redirect(url_for("home"))

        if request.method == "GET":
            return render_template("login.html")

        data = request.form
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")

        if not email or not password:
            return render_template(
                "login.html",
                error="Email and password are required."
            )

        client_ip = _client_ip()

        if ratelimit.is_locked("login", email, client_ip):
            remaining = ratelimit.lock_remaining_seconds("login", email, client_ip)
            logger.warning("Login locked out | Email: %s | IP: %s", email, client_ip)
            return render_template(
                "login.html",
                error=(
                    "Too many failed attempts. "
                    f"Try again in {remaining} seconds."
                ),
            ), 429

        # Consume the attempt budget before the password hash runs. Checking the
        # limit and recording the failure separately would let a burst of
        # parallel requests all pass the check before any of them records
        # anything, so the limiter would never engage at all.
        if not ratelimit.reserve("login", email, client_ip):
            ratelimit.lock("login", email, client_ip)
            remaining = ratelimit.lock_remaining_seconds("login", email, client_ip)
            logger.warning(
                "Login rate limit reached, locking account | Email: %s | IP: %s",
                email,
                client_ip,
            )
            return render_template(
                "login.html",
                error=(
                    "Too many failed attempts. "
                    f"Try again in {remaining} seconds."
                ),
            ), 429

        user = User.query.filter_by(email=email).first()
        if not user:
            return render_template(
                "login.html",
                error="Invalid email or password."
            )

        if not check_password_hash(user.password_hash, password):
            user.failed_login_count = (user.failed_login_count or 0) + 1
            db.session.commit()
            return render_template(
                "login.html",
                error="Invalid email or password."
            )

        if user.locked_until is not None:
            locked_until = user.locked_until
            if locked_until.tzinfo is None:
                locked_until = locked_until.replace(tzinfo=UTC)
            if locked_until > datetime.now(UTC):
                return render_template(
                    "login.html",
                    error=(
                        "This account is temporarily locked. "
                        f"Try again in {int((locked_until - datetime.now(UTC)).total_seconds())} seconds."
                    ),
                ), 429
            # The lockout has expired; clear it so the column does not linger.
            user.locked_until = None

        if (
            flask_app.config.get("REQUIRE_EMAIL_VERIFICATION")
            and not user.is_verified
        ):
            return render_template(
                "login.html",
                error=(
                    "Verify your email address before signing in. "
                    "Use the link we sent you, or request a new one below."
                ),
            ), 403

        ratelimit.clear("login", email, client_ip)
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = datetime.now(UTC)
        db.session.commit()

        session.clear()
        session["user_id"] = user.id
        session["username"] = user.username
        session["role"] = user.role

        logger.info("Login succeeded | User: %s | IP: %s", user.username, client_ip)
        return redirect(url_for("home"))

    @flask_app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @flask_app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "GET":
            return render_template("forgot_password.html")

        email = request.form.get("email", "").strip().lower()
        result = accounts.request_password_reset(email, requested_ip=_client_ip())

        logger.info("Password reset requested | Email: %s", email)

        # The same message is shown whether or not the address exists.
        return render_template("forgot_password.html", message=result["message"])

    @flask_app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password(token):
        if request.method == "GET":
            return render_template("reset_password.html", token=token)

        password = request.form.get("password", "")
        confirmation = request.form.get("confirm_password", "")

        result = accounts.reset_password(token, password, confirmation)

        if not result.get("ok"):
            return render_template(
                "reset_password.html",
                token=token,
                errors=result.get("errors", ["Unable to reset the password."]),
            ), 400

        _audit(
            "auth.password_reset",
            entity_type="user",
            entity_id=result["user"].id,
            summary=f"Password reset for {result['user'].username}",
        )

        return render_template(
            "reset_password.html",
            success="Your password has been updated. You can sign in now.",
        )

    @flask_app.route("/verify-email/<token>", methods=["GET"])
    def verify_email(token):
        result = accounts.verify_email(token)

        if not result.get("ok"):
            return render_template(
                "verify_email.html",
                message=result["message"],
                error=True,
            ), 400

        _audit(
            "auth.email_verified",
            entity_type="user",
            entity_id=result["user"].id,
            summary=f"Email verified for {result['user'].username}",
        )

        return render_template(
            "verify_email.html",
            message="Your email address has been verified.",
        )

    @flask_app.route("/api/me", methods=["GET"])
    @login_required
    def current_user():
        user = db.session.get(User, session["user_id"])

        if not user:
            session.clear()
            return jsonify({"error": "User not found"}), 401

        return jsonify({"user": user_to_dict(user)})

    @flask_app.route("/", methods=["GET"])
    @login_required
    def home():
        return render_template("index.html")


# ----------------------------------------------------------------------
# Authentication helpers
# ----------------------------------------------------------------------

def _client_ip() -> str | None:
    from services import audit

    return audit.client_ip()


def _first_user_role() -> str:
    """Decide the role for a new registration.

    The first account created on an empty database becomes the Admin. Reading
    the count and then inserting is separated by the password hash, roughly 80
    milliseconds, and the route is unauthenticated, so concurrent first signups
    could all observe an empty table and all create an Admin. The unique partial
    index on ``users.role`` (see :mod:`app.services.schema`) makes a second
    Admin impossible at the database level; this function only picks the
    intended value and the caller handles the resulting IntegrityError.
    """
    return "Admin" if User.query.count() == 0 else "Viewer"


def _audit(action: str, **kwargs) -> None:
    from services import audit

    try:
        audit.record(action, **kwargs)
    except Exception:  # pragma: no cover - audit never breaks a request
        db.session.rollback()


def _send_welcome_email(user) -> None:
    """Issue a verification token and try to deliver it. Never raises."""
    try:
        if not user.is_verified:
            token = accounts.issue_email_verification(user, requested_ip=_client_ip())
            accounts.deliver_verification_link(user, token)
    except Exception:  # pragma: no cover - defensive
        db.session.rollback()
        logger.exception("Welcome email failed | User: %s", user.username)


def _start_scheduler(flask_app: Flask) -> None:
    if flask_app.config.get("ENABLE_SCHEDULER"):
        scheduler.start(flask_app)


app = create_app()
_start_scheduler(app)


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", 5000)),
        debug=app.config.get("DEBUG", False),
        use_reloader=False,
        threaded=True
    )
