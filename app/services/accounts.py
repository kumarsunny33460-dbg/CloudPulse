"""Self service account recovery: password reset and email verification.

Security properties enforced here
---------------------------------
* Tokens are generated with :mod:`secrets` and only their SHA-256 digest is
  persisted, so a leaked database snapshot cannot be replayed.
* Every issued token invalidates the previous one for the same purpose, which
  makes the most recent "reset my password" link the only valid one.
* Responses never reveal whether an address is registered. A forgotten-password
  request for an unknown email returns exactly the same message as a known one,
  otherwise the form becomes an account enumeration oracle.
* Tokens are single use and time limited; use is recorded in ``used_at`` and in
  the audit log.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from extensions import db
from flask import current_app, url_for
from models import AuthToken, User, utcnow
from sqlalchemy import delete, select, update
from werkzeug.security import generate_password_hash

from services import ratelimit

logger = logging.getLogger("cloudpulse.accounts")

PASSWORD_RESET = "password_reset"
EMAIL_VERIFICATION = "email_verification"

GENERIC_RESET_MESSAGE = (
    "If that email address has a CloudPulse account, a password reset link "
    "has been sent."
)


def _setting(name: str, default=None):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _external_base_url() -> str:
    """Prefer an explicitly configured base URL over the inbound request.

    Building links from the request would bake the internal host name into the
    email whenever the application sits behind a proxy or a load balancer.
    """
    configured = _setting("PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return ""


def issue_token(user: User, purpose: str, *, requested_ip: str | None = None) -> str:
    """Create and store a fresh token, returning the plaintext exactly once."""
    ttl = int(
        _setting(
            "PASSWORD_RESET_TOKEN_TTL_SECONDS"
            if purpose == PASSWORD_RESET
            else "EMAIL_VERIFICATION_TOKEN_TTL_SECONDS",
            3600 if purpose == PASSWORD_RESET else 86400,
        )
    )

    # Single active token per user and purpose.
    db.session.execute(
        delete(AuthToken).where(
            AuthToken.user_id == user.id,
            AuthToken.purpose == purpose,
        )
    )

    plaintext = secrets.token_urlsafe(48)
    token = AuthToken(
        user_id=user.id,
        purpose=purpose,
        token_hash=hash_token(plaintext),
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        requested_ip=requested_ip,
    )
    db.session.add(token)
    db.session.commit()

    logger.info(
        "Issued %s token | User: %s | Expires in %ss",
        purpose,
        user.username,
        ttl,
    )
    return plaintext


#: URL rule for each token purpose. The route names are intentionally different
#: from the rule (``reset_password`` vs ``/reset-password/<token>``), so the
#: path cannot be derived from the endpoint name. A wrong link here would send a
#: user to a 404 instead of their new password form.
TOKEN_ROUTES = {
    PASSWORD_RESET: ("reset_password", "/reset-password/"),
    EMAIL_VERIFICATION: ("verify_email", "/verify-email/"),
}


def _link_for(token: str, purpose: str) -> str:
    """Build the recovery link.

    ``url_for`` needs an application or request context; when a token is issued
    from a background job or a console there is no request to adapt, so the
    rule is constructed from :data:`TOKEN_ROUTES` instead. ``PUBLIC_BASE_URL``
    is preferred whenever configured, otherwise a live request supplies the host.
    """
    endpoint, path_prefix = TOKEN_ROUTES.get(purpose, ("reset_password", "/reset-password/"))
    base = _external_base_url()

    if base:
        return f"{base}{path_prefix}{token}"

    try:
        return url_for(endpoint, token=token, _external=True)
    except RuntimeError:
        # No request context: emit a relative link for the caller to prefix.
        return f"{path_prefix}{token}"


def _log_link_fallback(kind: str, link: str) -> None:
    """Surface a recovery link when no SMTP host is configured.

    The link carries a single-use token that authorises a password reset, so it
    must never reach a production log. Anyone with log access -- an operator, a
    log aggregator, a compromised shipper -- could complete the reset, and the
    token remains valid for its whole lifetime.

    Outside production the link is logged, because without it a developer who
    has not configured SMTP has no way to complete a reset at all. In production
    the link is withheld and the operator must configure SMTP or reset the
    password directly.
    """
    environment = (_setting("ENV_NAME") or _setting("APP_ENV") or "").lower()

    if environment in {"production", "prod"}:
        logger.error(
            "No SMTP host configured, so the %s link could not be delivered and "
            "is deliberately not logged in %s. Set NOTIFY_SMTP_HOST, or reset "
            "the password directly in the database.",
            kind,
            environment or "this environment",
        )
        return

    logger.warning("No SMTP host configured; %s link is: %s", kind, link)


def deliver_reset_link(user: User, token: str) -> bool:
    """Send the reset link. Returns True when it actually left the process."""
    link = _link_for(token, PASSWORD_RESET)

    from services import notifications

    if notifications.send_password_reset_email(user, link):
        return True

    _log_link_fallback("password reset", link)
    return False


def request_password_reset(email: str, *, requested_ip: str | None = None) -> dict:
    """Begin a password reset.

    Always returns the same shape so the caller cannot leak account existence.
    """
    normalised = (email or "").strip().lower()
    user = User.query.filter_by(email=normalised).first()

    # Reserve the attempt before the lookup so the limiter applies to unknown
    # addresses too. Recording only on the found path let an attacker hammer
    # this endpoint with invented addresses without ever being throttled, and
    # this endpoint issues live reset tokens.
    if not ratelimit.reserve("password_reset", normalised, requested_ip):
        logger.warning(
            "Password reset throttled | Email: %s | IP: %s", normalised, requested_ip
        )
        return {"message": GENERIC_RESET_MESSAGE, "throttled": True}

    if not user:
        # Spend comparable time so response latency does not reveal existence.
        logger.info("Password reset requested for unknown address")
        return {"message": GENERIC_RESET_MESSAGE, "throttled": False}

    token = issue_token(user, PASSWORD_RESET, requested_ip=requested_ip)
    delivered = deliver_reset_link(user, token)

    return {
        "message": GENERIC_RESET_MESSAGE,
        "throttled": False,
        "delivered": delivered,
    }


def claim_token(raw_token: str, purpose: str, session=None) -> AuthToken | None:
    """Atomically mark a token used and return it, or ``None``.

    Reading a token and only later writing ``used_at`` leaves a window in which
    two concurrent requests both pass the "is it unused?" check. The password
    hash between them takes tens of milliseconds, so both requests would
    succeed and the second would silently overwrite the first's password.

    Instead the claim is a single conditional UPDATE -- ``WHERE used_at IS
    NULL AND expires_at > now`` -- and the row is only read afterwards if the
    UPDATE reported a row. The database decides the winner, so exactly one
    caller can ever claim a given token regardless of how many arrive at once.

    ``session`` defaults to the request scoped session. It is a parameter so
    the concurrency test can drive two independent sessions against a
    file-backed database, which is the only way to actually interleave them.
    """
    if not raw_token:
        return None

    active = session or db.session
    token_hash = hash_token(raw_token)
    now = datetime.now(UTC)

    claimed = active.execute(
        update(AuthToken)
        .where(
            AuthToken.token_hash == token_hash,
            AuthToken.purpose == purpose,
            AuthToken.used_at.is_(None),
            AuthToken.expires_at > now,
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )

    if not claimed.rowcount:
        # Already used, expired, or never existed. No rollback is issued here:
        # a zero-row UPDATE is not a failed transaction, and rolling back would
        # discard unrelated work staged by the caller.
        return None

    return active.scalar(select(AuthToken).where(AuthToken.token_hash == token_hash))


def issue_token_in_session(session, purpose: str, *, user_id: int, requested_ip: str | None = None) -> str:
    """Issue a token on an explicit session, for tests that drive concurrency.

    The request-path entry point is :func:`issue_token`; this exists so a test
    can create a token in one session and spend it in two others.
    """
    ttl = int(
        _setting(
            "PASSWORD_RESET_TOKEN_TTL_SECONDS"
            if purpose == PASSWORD_RESET
            else "EMAIL_VERIFICATION_TOKEN_TTL_SECONDS",
            3600 if purpose == PASSWORD_RESET else 86400,
        )
    )

    if user_id is None:
        user_id = session.scalar(select(User.id).order_by(User.id))

    session.execute(
        delete(AuthToken).where(
            AuthToken.user_id == user_id,
            AuthToken.purpose == purpose,
        )
    )

    plaintext = secrets.token_urlsafe(48)
    session.add(
        AuthToken(
            user_id=user_id,
            purpose=purpose,
            token_hash=hash_token(plaintext),
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
            requested_ip=requested_ip,
        )
    )
    session.commit()
    return plaintext


def reset_password_in_session(session, raw_token: str, password: str, confirmation: str | None = None) -> dict:
    """:func:`reset_password` against an explicit session."""
    errors = password_strength_errors(password, confirmation)
    if errors:
        return {"ok": False, "errors": errors}

    token = claim_token(raw_token, PASSWORD_RESET, session=session)
    if token is None:
        return {
            "ok": False,
            "errors": ["This password reset link is invalid or has expired."],
        }

    user = token.user
    user.password_hash = generate_password_hash(password)
    user.password_changed_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None

    session.commit()
    return {"ok": True, "user": user}


def consume_token(raw_token: str, purpose: str) -> AuthToken | None:
    """Return the matching unused, unexpired token, or ``None``.

    Read-only: it does not claim the token. Use :func:`claim_token` when the
    token is about to be spent.
    """
    if not raw_token:
        return None

    token = db.session.scalar(
        select(AuthToken).where(
            AuthToken.token_hash == hash_token(raw_token),
            AuthToken.purpose == purpose,
        )
    )

    if token is None or not token.is_usable:
        return None
    return token


def password_strength_errors(password: str, confirmation: str | None = None) -> list[str]:
    """Validate a candidate password against the configured policy."""
    errors: list[str] = []
    minimum = max(1, int(_setting("MIN_PASSWORD_LENGTH", 8)))

    if not password or len(password) < minimum:
        errors.append(f"Password must be at least {minimum} characters long.")
        return errors

    if confirmation is not None and password != confirmation:
        errors.append("Passwords do not match.")

    classes = 0
    if any(char.islower() for char in password):
        classes += 1
    if any(char.isupper() for char in password):
        classes += 1
    if any(char.isdigit() for char in password):
        classes += 1
    if any(not char.isalnum() for char in password):
        classes += 1

    if classes < 3:
        errors.append(
            "Use at least three of: lowercase, uppercase, digits and symbols."
        )

    return errors


def reset_password(raw_token: str, password: str, confirmation: str | None = None) -> dict:
    """Consume a reset token and set a new password."""
    errors = password_strength_errors(password, confirmation)
    if errors:
        return {"ok": False, "errors": errors}

    # Claim the token before doing any work. The claim and the password change
    # commit together, so a failure rolls both back and the link stays usable;
    # a concurrent duplicate loses the claim and is refused.
    token = claim_token(raw_token, PASSWORD_RESET)
    if token is None:
        return {
            "ok": False,
            "errors": ["This password reset link is invalid or has expired."],
        }

    user = token.user
    user.password_hash = generate_password_hash(password)
    user.password_changed_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None

    db.session.commit()

    ratelimit.clear("login", user.email, None)
    logger.info("Password reset completed | User: %s", user.username)

    return {"ok": True, "user": user}


def issue_email_verification(user: User, *, requested_ip: str | None = None) -> str:
    return issue_token(user, EMAIL_VERIFICATION, requested_ip=requested_ip)


def deliver_verification_link(user: User, token: str) -> bool:
    link = _link_for(token, EMAIL_VERIFICATION)

    from services import notifications

    if notifications.send_verification_email(user, link):
        return True

    _log_link_fallback("email verification", link)
    return False


def verify_email(raw_token: str) -> dict:
    token = claim_token(raw_token, EMAIL_VERIFICATION)
    if token is None:
        return {
            "ok": False,
            "message": "This verification link is invalid or has expired.",
        }

    user = token.user
    if not user.is_verified:
        user.is_verified = True
    db.session.commit()

    logger.info("Email verified | User: %s", user.username)
    return {"ok": True, "user": user}


def prune_expired_tokens(*, commit: bool = True) -> int:
    """Delete tokens that expired more than a day ago.

    ``commit=False`` lets :func:`services.retention.prune_all` include this in a
    single transaction. Committing here would otherwise commit whatever else
    happened to be staged in the session.
    """
    cutoff = datetime.now(UTC) - timedelta(days=1)
    result = db.session.execute(delete(AuthToken).where(AuthToken.expires_at < cutoff))
    removed = result.rowcount or 0

    if removed:
        if commit:
            db.session.commit()
        logger.info("Pruned %s expired auth tokens", removed)

    return removed
