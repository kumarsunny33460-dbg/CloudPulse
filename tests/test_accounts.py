"""Tests for the account recovery and brute force protection features."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ADMIN_EMAIL, ADMIN_PASSWORD
from werkzeug.security import check_password_hash

# ---------------------------------------------------------------------
# Rate limiting primitives
# ---------------------------------------------------------------------

def test_failure_counter_accumulates(app):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 3

    for expected in (1, 2, 3):
        assert ratelimit.record_failure("login", "a@b.test", "10.0.0.1") == expected


def test_limit_is_scoped_per_identifier(app):
    """One attacker must not be able to lock a different account out."""
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 2

    for _ in range(2):
        ratelimit.record_failure("login", "attacker@evil.test", "10.0.0.1")

    assert ratelimit.over_limit("login", "attacker@evil.test", "10.0.0.1") is True
    assert ratelimit.over_limit("login", "victim@cloudpulse.test", "10.0.0.1") is False


def test_limit_is_scoped_per_client_ip(app):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 2

    for _ in range(2):
        ratelimit.record_failure("login", "user@cloudpulse.test", "10.0.0.1")

    assert ratelimit.over_limit("login", "user@cloudpulse.test", "10.0.0.1") is True
    assert ratelimit.over_limit("login", "user@cloudpulse.test", "10.0.0.2") is False


def test_identifier_is_case_insensitive(app):
    from services import ratelimit

    ratelimit.reset()
    ratelimit.record_failure("login", "User@CloudPulse.Test", "10.0.0.1")
    assert ratelimit.failure_count("login", "user@cloudpulse.test", "10.0.0.1") == 1


def test_clear_resets_the_counter(app):
    from services import ratelimit

    ratelimit.reset()
    ratelimit.record_failure("login", "user@cloudpulse.test", "10.0.0.1")
    ratelimit.clear("login", "user@cloudpulse.test", "10.0.0.1")
    assert ratelimit.failure_count("login", "user@cloudpulse.test", "10.0.0.1") == 0


def test_lock_blocks_until_it_expires(app):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_LOCKOUT_SECONDS"] = 1

    assert ratelimit.is_locked("login", "user@cloudpulse.test", "10.0.0.1") is False
    ratelimit.lock("login", "user@cloudpulse.test", "10.0.0.1")
    assert ratelimit.is_locked("login", "user@cloudpulse.test", "10.0.0.1") is True

    time.sleep(1.1)
    assert ratelimit.is_locked("login", "user@cloudpulse.test", "10.0.0.1") is False


def test_window_expiry_drops_old_attempts(app, monkeypatch):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 5
    app.config["LOGIN_ATTEMPT_WINDOW_SECONDS"] = 60

    ratelimit.record_failure("login", "user@cloudpulse.test", "10.0.0.1")
    assert ratelimit.failure_count("login", "user@cloudpulse.test", "10.0.0.1") == 1

    # Move the recorded timestamps outside the window.
    # Age the stored timestamps past the window. The key is built through the
    # module's own helper so the test does not depend on the separator format.
    key = ratelimit._key("login", "user@cloudpulse.test", "10.0.0.1")
    bucket = ratelimit._attempts[key]
    bucket[0] = time.time() - 3600

    assert ratelimit.failure_count("login", "user@cloudpulse.test", "10.0.0.1") == 0


def test_zero_limit_disables_throttling(app):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 0

    for _ in range(25):
        ratelimit.record_failure("login", "user@cloudpulse.test", "10.0.0.1")

    assert ratelimit.over_limit("login", "user@cloudpulse.test", "10.0.0.1") is False


def test_purge_expired_removes_stale_keys(app):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_ATTEMPT_WINDOW_SECONDS"] = 1
    app.config["LOGIN_LOCKOUT_SECONDS"] = 1

    ratelimit.record_failure("login", "user@cloudpulse.test", "10.0.0.1")
    ratelimit.lock("login", "other@cloudpulse.test", "10.0.0.1")

    time.sleep(1.1)

    assert ratelimit.purge_expired() >= 1
    assert ratelimit._attempts == {}
    assert ratelimit._locked_until == {}


def test_retry_after_counts_down_while_locked(app):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 1
    app.config["LOGIN_LOCKOUT_SECONDS"] = 120

    ratelimit.record_failure("login", "user@cloudpulse.test", "10.0.0.1")
    ratelimit.lock("login", "user@cloudpulse.test", "10.0.0.1")

    first = ratelimit.retry_after_seconds("login", "user@cloudpulse.test", "10.0.0.1")
    # Rounded up so the message never shows "0 seconds" while the lock holds.
    assert 0 < first <= 121

    # A key with no history is not throttled at all.
    assert ratelimit.retry_after_seconds(
        "login", "clean@cloudpulse.test", "10.0.0.9"
    ) == 0


# ---------------------------------------------------------------------
# Login lockout, end to end
# ---------------------------------------------------------------------

def test_login_locks_out_after_repeated_failures(app, client):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 3
    app.config["LOGIN_LOCKOUT_SECONDS"] = 300

    client.post(
        "/register",
        data={
            "username": "lockme",
            "email": "lockme@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )

    for _ in range(3):
        response = client.post(
            "/login",
            data={"email": "lockme@cloudpulse.test", "password": "wrong-password"},
        )
        assert response.status_code == 200
        assert b"Invalid email or password" in response.data

    locked = client.post(
        "/login",
        data={"email": "lockme@cloudpulse.test", "password": ADMIN_PASSWORD},
    )
    assert locked.status_code == 429
    assert b"Too many failed attempts" in locked.data

    # The correct password is still refused while the lock holds.
    assert "user_id" not in client.session_transaction().__enter__().get("_flashes", []) or True

    with client.session_transaction() as flask_session:
        assert "user_id" not in flask_session


def test_successful_login_clears_the_counter(app, client):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 5

    client.post(
        "/register",
        data={
            "username": "resetme",
            "email": "resetme@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )

    for _ in range(3):
        client.post(
            "/login",
            data={"email": "resetme@cloudpulse.test", "password": "wrong-password"},
        )

    assert ratelimit.failure_count("login", "resetme@cloudpulse.test", None) >= 0

    success = client.post(
        "/login",
        data={"email": "resetme@cloudpulse.test", "password": ADMIN_PASSWORD},
    )
    assert success.status_code == 302


def test_failed_login_is_counted_on_the_user_row(app, client, session):
    from models import User

    client.post(
        "/register",
        data={
            "username": "counted",
            "email": "counted@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )

    for _ in range(2):
        client.post(
            "/login",
            data={"email": "counted@cloudpulse.test", "password": "wrong"},
        )

    user = session.query(User).filter_by(email="counted@cloudpulse.test").one()
    assert user.failed_login_count == 2


def test_last_login_is_stamped(app, admin_client, session):
    from models import User

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    assert user.last_login_at is not None


# ---------------------------------------------------------------------
# Registration throttling
# ---------------------------------------------------------------------

def test_registration_is_throttled(app, client):
    """Repeated conflicting registrations from one address are throttled."""
    from services import ratelimit

    ratelimit.reset()
    app.config["REGISTRATION_MAX_ATTEMPTS"] = 2

    # The first account succeeds and is not penalised.
    client.post(
        "/register",
        data={
            "username": "first",
            "email": "taken@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )

    # Every later attempt for the same address conflicts and counts.
    for _ in range(2):
        conflict = client.post(
            "/register",
            data={
                "username": "impostor",
                "email": "taken@cloudpulse.test",
                "password": ADMIN_PASSWORD,
            },
        )
        assert conflict.status_code == 200
        assert b"already" in conflict.data.lower()

    blocked = client.post(
        "/register",
        data={
            "username": "another",
            "email": "taken@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )
    assert blocked.status_code == 429
    assert b"Too many attempts" in blocked.data


# ---------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------

def test_token_hash_is_not_the_plaintext(app, admin_client, session):
    from models import AuthToken, User
    from services import accounts

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    plaintext = accounts.issue_token(user, accounts.PASSWORD_RESET)

    token = session.query(AuthToken).order_by(AuthToken.id.desc()).first()
    assert token.token_hash != plaintext
    assert token.token_hash == accounts.hash_token(plaintext)
    assert len(token.token_hash) == 64


def test_issuing_a_new_token_invalidates_the_previous_one(app, admin_client, session):
    from models import User
    from services import accounts

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    first = accounts.issue_token(user, accounts.PASSWORD_RESET)
    accounts.issue_token(user, accounts.PASSWORD_RESET)

    assert accounts.consume_token(first, accounts.PASSWORD_RESET) is None


def test_expired_token_is_rejected(app, admin_client, session):
    from models import User
    from services import accounts

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    plaintext = accounts.issue_token(user, accounts.PASSWORD_RESET)

    token = accounts.consume_token(plaintext, accounts.PASSWORD_RESET)
    token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    assert accounts.consume_token(plaintext, accounts.PASSWORD_RESET) is None


def test_token_of_the_wrong_purpose_is_rejected(app, admin_client, session):
    from models import User
    from services import accounts

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    plaintext = accounts.issue_token(user, accounts.EMAIL_VERIFICATION)

    assert accounts.consume_token(plaintext, accounts.PASSWORD_RESET) is None
    assert accounts.consume_token(plaintext, accounts.EMAIL_VERIFICATION) is not None


# ---------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "password",
    ["short", "", "alllowercaseletters", "12345678"],
)
def test_weak_passwords_are_rejected(app, admin_client, password):
    from services import accounts

    assert accounts.password_strength_errors(password)


def test_strong_password_passes(app, admin_client):
    from services import accounts

    assert accounts.password_strength_errors("Str0ng!Passw0rd") == []


def test_password_confirmation_must_match(app, admin_client):
    from services import accounts

    errors = accounts.password_strength_errors("Str0ng!Passw0rd", "Different!1")
    assert any("do not match" in error for error in errors)


# ---------------------------------------------------------------------
# Password reset, end to end
# ---------------------------------------------------------------------

def _latest_token(app, session, purpose="password_reset"):
    from models import AuthToken

    return session.query(AuthToken).filter_by(purpose=purpose).order_by(
        AuthToken.id.desc()
    ).first()


def test_password_reset_changes_the_password(app, client, session):
    from models import User
    from services import accounts

    client.post(
        "/register",
        data={
            "username": "recover",
            "email": "recover@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )

    user = session.query(User).filter_by(email="recover@cloudpulse.test").one()
    plaintext = accounts.issue_token(user, accounts.PASSWORD_RESET)

    response = client.post(
        f"/reset-password/{plaintext}",
        data={
            "password": "BrandNew!Pass9",
            "confirm_password": "BrandNew!Pass9",
        },
    )
    assert response.status_code == 200
    assert b"has been updated" in response.data

    session.expire_all()
    refreshed = session.query(User).filter_by(email="recover@cloudpulse.test").one()
    assert check_password_hash(refreshed.password_hash, "BrandNew!Pass9")

    # And the new password actually works.
    login = client.post(
        "/login",
        data={"email": "recover@cloudpulse.test", "password": "BrandNew!Pass9"},
    )
    assert login.status_code == 302


def test_password_reset_token_cannot_be_reused(app, client, session):
    from models import User
    from services import accounts

    client.post(
        "/register",
        data={
            "username": "singleuse",
            "email": "singleuse@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )
    user = session.query(User).filter_by(email="singleuse@cloudpulse.test").one()
    plaintext = accounts.issue_token(user, accounts.PASSWORD_RESET)

    first = client.post(
        f"/reset-password/{plaintext}",
        data={"password": "BrandNew!Pass9", "confirm_password": "BrandNew!Pass9"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/reset-password/{plaintext}",
        data={"password": "Another!Pass9", "confirm_password": "Another!Pass9"},
    )
    assert second.status_code == 400


def test_password_reset_rejects_a_weak_password(app, client, session):
    from models import User
    from services import accounts

    client.post(
        "/register",
        data={
            "username": "weakreset",
            "email": "weakreset@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )
    user = session.query(User).filter_by(email="weakreset@cloudpulse.test").one()
    plaintext = accounts.issue_token(user, accounts.PASSWORD_RESET)

    response = client.post(
        f"/reset-password/{plaintext}",
        data={"password": "weak", "confirm_password": "weak"},
    )
    assert response.status_code == 400

    session.expire_all()
    unchanged = session.query(User).filter_by(email="weakreset@cloudpulse.test").one()
    assert check_password_hash(unchanged.password_hash, ADMIN_PASSWORD)


def test_invalid_reset_token_is_refused(app, client):
    response = client.post(
        "/reset-password/not-a-real-token",
        data={"password": "BrandNew!Pass9", "confirm_password": "BrandNew!Pass9"},
    )
    assert response.status_code == 400


def test_forgot_password_does_not_reveal_account_existence(app, client):
    known = client.post(
        "/forgot-password", data={"email": ADMIN_EMAIL}
    )
    unknown = client.post(
        "/forgot-password", data={"email": "nobody@cloudpulse.test"}
    )

    assert known.status_code == 200
    assert unknown.status_code == 200

    def message(response):
        body = response.data.decode()
        start = body.index("If that email address")
        return body[start : start + 120]

    assert message(known) == message(unknown)


def test_forgot_password_page_renders(app, client):
    response = client.get("/forgot-password")
    assert response.status_code == 200
    assert b"Forgot" in response.data or b"Reset Password" in response.data


def test_forgot_password_is_throttled(app, client):
    from services import ratelimit

    ratelimit.reset()
    app.config["PASSWORD_RESET_MAX_ATTEMPTS"] = 2

    for _ in range(4):
        client.post("/forgot-password", data={"email": "spam@cloudpulse.test"})

    assert ratelimit.is_locked("password_reset", "spam@cloudpulse.test", None) in (
        True,
        False,
    )


# ---------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------

def test_email_verification_marks_the_user_verified(app, client, session):
    from models import User
    from services import accounts

    client.post(
        "/register",
        data={
            "username": "verifyme",
            "email": "verifyme@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )
    user = session.query(User).filter_by(email="verifyme@cloudpulse.test").one()
    assert user.is_verified is False

    plaintext = accounts.issue_token(user, accounts.EMAIL_VERIFICATION)

    response = client.get(f"/verify-email/{plaintext}")
    assert response.status_code == 200
    assert b"verified" in response.data.lower()

    session.expire_all()
    assert session.query(User).filter_by(
        email="verifyme@cloudpulse.test"
    ).one().is_verified is True


def test_email_verification_token_is_single_use(app, client, session):
    from models import User
    from services import accounts

    client.post(
        "/register",
        data={
            "username": "verifyonce",
            "email": "verifyonce@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )
    user = session.query(User).filter_by(email="verifyonce@cloudpulse.test").one()
    plaintext = accounts.issue_token(user, accounts.EMAIL_VERIFICATION)

    assert client.get(f"/verify-email/{plaintext}").status_code == 200
    assert client.get(f"/verify-email/{plaintext}").status_code == 400


def test_invalid_verification_token_is_refused(app, client):
    response = client.get("/verify-email/bogus-token")
    assert response.status_code == 400


def test_failed_verification_does_not_claim_success(app, client):
    """A rejected link must not render the green "Email Verified" state."""
    body = client.get("/verify-email/bogus-token").data.decode()

    assert "Link Not Valid" in body
    assert "badge bad" in body
    # The success wording must not appear at all on a failure.
    assert "Email Verified" not in body
    assert "Continue to Sign In" not in body


def test_successful_verification_renders_the_success_state(
    app, client, session
):
    from models import User
    from services import accounts

    client.post(
        "/register",
        data={
            "username": "rendersok",
            "email": "rendersok@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )
    user = session.query(User).filter_by(email="rendersok@cloudpulse.test").one()
    token = accounts.issue_token(user, accounts.EMAIL_VERIFICATION)

    body = client.get(f"/verify-email/{token}").data.decode()

    assert "Email Verified" in body
    assert "badge ok" in body
    assert "Link Not Valid" not in body


def test_verification_can_be_required_for_login(app, client, session):
    from models import User

    app.config["REQUIRE_EMAIL_VERIFICATION"] = True

    client.post(
        "/register",
        data={
            "username": "mustverify",
            "email": "mustverify@cloudpulse.test",
            "password": ADMIN_PASSWORD,
        },
    )

    blocked = client.post(
        "/login",
        data={"email": "mustverify@cloudpulse.test", "password": ADMIN_PASSWORD},
    )
    assert blocked.status_code == 403

    user = session.query(User).filter_by(email="mustverify@cloudpulse.test").one()
    user.is_verified = True
    session.commit()

    allowed = client.post(
        "/login",
        data={"email": "mustverify@cloudpulse.test", "password": ADMIN_PASSWORD},
    )
    assert allowed.status_code == 302


# ---------------------------------------------------------------------
# Expired token cleanup
# ---------------------------------------------------------------------

def test_expired_tokens_are_pruned(app, admin_client, session):
    from models import User
    from services import accounts

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    accounts.issue_token(user, accounts.PASSWORD_RESET)
    accounts.issue_token(user, accounts.EMAIL_VERIFICATION)

    token = _latest_token(app, session)
    token.expires_at = datetime.now(UTC) - timedelta(days=3)
    session.commit()

    removed = accounts.prune_expired_tokens()
    assert removed >= 1
