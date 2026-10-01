"""Regression tests for the security findings from the independent review.

Each test names the finding it pins. They are grouped by root cause rather
than by symptom, because several findings shared one cause.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading

import pytest
from conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    JSON_HEADERS,
)
from conftest import (
    TEST_CLIENT_IP as PEER_IP,
)


@contextlib.contextmanager
def request_context(app, headers=None):
    """A request context with a known peer address."""
    with app.test_request_context(
        "/", environ_base={"REMOTE_ADDR": PEER_IP}, headers=headers or {}
    ):
        yield


def test_testing_config_still_gets_an_in_memory_database():
    """Regression: the dynamic config pass must not clobber an explicit URI.

    It once overwrote TestingConfig's sqlite:// with the development database
    file, so the suite's drop_all() wiped the developer's real data.
    """
    from app import create_app

    application = create_app("testing")

    assert application.config["SQLALCHEMY_DATABASE_URI"] == "sqlite://"
    assert "instance" not in (application.config["SQLALCHEMY_DATABASE_URI"] or "")


def test_dynamic_config_fills_only_the_placeholders():
    import config as config_module

    settings = {"SECRET_KEY": "explicit-key", "SQLALCHEMY_DATABASE_URI": "sqlite://"}

    config_module.apply_dynamic_config(settings)

    assert settings["SECRET_KEY"] == "explicit-key"
    assert settings["SQLALCHEMY_DATABASE_URI"] == "sqlite://"


def test_dynamic_config_fills_a_placeholder(monkeypatch):
    import config as config_module

    monkeypatch.setenv("SECRET_KEY", "from-env")
    settings = {"SECRET_KEY": None, "SQLALCHEMY_DATABASE_URI": None}

    config_module.apply_dynamic_config(settings)

    assert settings["SECRET_KEY"] == "from-env"
    assert settings["SQLALCHEMY_DATABASE_URI"]


# ======================================================================
# F1 - X-Forwarded-For is attacker controlled
#
# The rate limit key included the client IP, which was taken verbatim from
# X-Forwarded-For. Rotating the header minted a fresh bucket on every request,
# so the limiter never engaged at all.
# ======================================================================


def test_forwarded_for_is_ignored_when_no_proxy_is_trusted(app):
    from services import audit

    app.config["TRUSTED_PROXY_NETWORKS"] = ""

    with request_context(app, {"X-Forwarded-For": "1.2.3.4"}):
        # The socket peer is the only trustworthy value.
        assert audit.client_ip() == PEER_IP


def test_forwarded_for_cannot_cycle_the_rate_limit_bucket(app, client):
    """The exact bypass: rotate the header on every attempt."""
    from services import ratelimit

    ratelimit.reset()
    app.config["TRUSTED_PROXY_NETWORKS"] = ""
    app.config["LOGIN_MAX_ATTEMPTS"] = 3
    app.config["STRICT_ORIGIN_CHECK"] = False

    statuses = []
    for octet in range(1, 9):
        response = client.post(
            "/login",
            data={"email": "victim@cloudpulse.test", "password": "wrong"},
            headers={"X-Forwarded-For": f"10.0.0.{octet}"},
        )
        statuses.append(response.status_code)

    assert 429 in statuses, f"limiter never engaged: {statuses}"


def test_forwarded_for_is_trusted_from_a_configured_proxy(app):
    from services import audit

    app.config["TRUSTED_PROXY_NETWORKS"] = f"{PEER_IP}/32"

    with request_context(app, {"X-Forwarded-For": "203.0.113.9"}):
        assert audit.client_ip() == "203.0.113.9"


def test_forwarded_chain_is_walked_past_trusted_proxies(app):
    from services import audit

    app.config["TRUSTED_PROXY_NETWORKS"] = f"{PEER_IP}/32,10.0.0.0/8"

    with request_context(app, {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}):
        # The rightmost address is our own proxy, so the real client is the
        # next one along, not the header's first entry.
        assert audit.client_ip() == "203.0.113.9"


def test_a_spoofed_chain_ending_at_the_peer_is_ignored(app):
    from services import audit

    app.config["TRUSTED_PROXY_NETWORKS"] = f"{PEER_IP}/32,10.0.0.0/8"

    # Every entry is a trusted proxy, so nothing in the chain identifies a
    # client and the peer address is used instead of trusting the header.
    with request_context(app, {"X-Forwarded-For": "10.9.9.9"}):
        assert audit.client_ip() == PEER_IP


# ======================================================================
# F2 - check and record were not atomic
#
# over_limit() and record_failure() each took the lock separately, with an
# ~80ms password hash in between. Parallel requests all passed the check
# before any recorded a failure.
# ======================================================================


def test_reserve_consumes_the_budget_before_the_hash_runs(app):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 3

    assert [ratelimit.reserve("login", "a@b.test", "1.1.1.1") for _ in range(5)] == [
        True,
        True,
        True,
        False,
        False,
    ]


def test_parallel_login_attempts_cannot_exceed_the_limit(app, client):
    """Threads all pass the check together; the reservation must stop them."""
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 5
    app.config["LOGIN_LOCKOUT_SECONDS"] = 300
    app.config["STRICT_ORIGIN_CHECK"] = False

    statuses: list[int] = []
    lock = threading.Lock()
    # Each thread gets its own client; a single test client is not thread safe.
    clients = [app.test_client() for _ in range(12)]

    def attempt(index: int) -> None:
        response = clients[index].post(
            "/login",
            data={"email": "victim@cloudpulse.test", "password": f"wrong{index}"},
        )
        with lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    accepted = statuses.count(200)
    assert accepted <= 5, f"{accepted} wrong passwords were checked, limit is 5"


def test_reserve_is_a_no_op_when_the_limit_is_zero(app):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 0

    for _ in range(20):
        assert ratelimit.reserve("login", "a@b.test", "1.1.1.1") is True


def test_successful_login_returns_the_reserved_budget(app, client):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 5
    app.config["STRICT_ORIGIN_CHECK"] = False

    client.post(
        "/register",
        data={
            "username": "refund",
            "email": "refund@cloudpulse.test",
            "password": "Str0ng!Passw0rd",
        },
    )

    for _ in range(3):
        client.post(
            "/login",
            data={"email": "refund@cloudpulse.test", "password": "wrong"},
        )

    assert ratelimit.failure_count("login", "refund@cloudpulse.test", PEER_IP) == 3

    response = client.post(
        "/login",
        data={"email": "refund@cloudpulse.test", "password": "Str0ng!Passw0rd"},
    )
    assert response.status_code == 302
    assert ratelimit.failure_count("login", "refund@cloudpulse.test", PEER_IP) == 0


# ======================================================================
# F3 / F4 - SECRET_KEY frozen before load_dotenv, and ENV_NAME disagreeing
#           with the config that was actually selected
# ======================================================================


def test_secret_key_supplied_after_import_is_still_used(monkeypatch):
    """A .env value must win over the ephemeral key resolved at import."""
    import importlib

    import config as config_module

    monkeypatch.setenv("SECRET_KEY", "key-from-the-environment")
    importlib.reload(config_module)

    class Holder:
        pass

    holder = Holder()
    holder.__dict__["SECRET_KEY"] = None
    config_module.apply_dynamic_config(holder.__dict__)

    assert holder.SECRET_KEY == "key-from-the-environment"


def test_unknown_environment_is_refused(monkeypatch):
    """A typo must not silently start the app in debug mode."""
    import config as config_module

    monkeypatch.setenv("APP_ENV", "prodction")

    with pytest.raises(RuntimeError, match="Unknown APP_ENV"):
        config_module.get_config()


def test_known_environments_still_resolve(monkeypatch):
    import config as config_module

    for name in ("development", "testing", "production"):
        monkeypatch.setenv("APP_ENV", name)
        assert config_module.get_config() is not None


def test_production_without_a_secret_key_refuses_to_boot(monkeypatch):
    import config as config_module

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        config_module.validate_config("production", {"STRICT_ORIGIN_CHECK": True})


def test_env_name_matches_the_selected_config(monkeypatch):
    """ENV_NAME and the config class must never disagree.

    They did once: a Render deployment selected ProductionConfig while
    recording ENV_NAME as "development", so validate_config returned early and
    the SECRET_KEY guard never ran.
    """
    import importlib

    import config as config_module

    from app import create_app

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("APP_ENV", raising=False)
    # Long enough to clear the production key check, which is the point of a
    # separate test and would otherwise mask what this one is asserting.
    real_key = "R" * 48
    monkeypatch.setenv("SECRET_KEY", real_key)
    importlib.reload(config_module)

    application = create_app()

    assert application.config["ENV_NAME"] == "production"
    assert application.config["SESSION_COOKIE_SECURE"] is True
    assert application.config["SECRET_KEY"] == real_key


# ======================================================================
# F5 - concurrent first signups could all become Admin
# ======================================================================


def test_only_one_admin_can_exist(app, session):
    from models import User
    from sqlalchemy.exc import IntegrityError
    from werkzeug.security import generate_password_hash

    session.add(
        User(
            username="first",
            email="first@cloudpulse.test",
            password_hash=generate_password_hash(ADMIN_PASSWORD),
            role="Admin",
        )
    )
    session.commit()

    session.add(
        User(
            username="second",
            email="second@cloudpulse.test",
            password_hash=generate_password_hash(ADMIN_PASSWORD),
            role="Admin",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    assert session.query(User).filter_by(role="Admin").count() == 1


def test_a_second_registration_is_not_an_admin(app, client, session):
    from models import User

    client.post(
        "/register",
        data={
            "username": "firstadmin",
            "email": "firstadmin@cloudpulse.test",
            "password": "Str0ng!Passw0rd",
        },
    )
    client.post(
        "/register",
        data={
            "username": "seconduser",
            "email": "seconduser@cloudpulse.test",
            "password": "Str0ng!Passw0rd",
        },
    )

    roles = {
        user.username: user.role
        for user in session.query(User).filter(
            User.email.in_(
                ["firstadmin@cloudpulse.test", "seconduser@cloudpulse.test"]
            )
        )
    }
    assert roles["firstadmin"] == "Admin"
    assert roles["seconduser"] == "Viewer"


# ======================================================================
# F6 - reset tokens were written to the application log
# ======================================================================


def test_reset_token_is_never_logged_in_production(app, admin_client, session, caplog):
    from models import User
    from services import accounts

    app.config["ENV_NAME"] = "production"
    app.config["NOTIFY_SMTP_HOST"] = None

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    token = accounts.issue_token(user, accounts.PASSWORD_RESET)

    with caplog.at_level("DEBUG"):
        accounts.deliver_reset_link(user, token)

    assert token not in caplog.text


def test_verification_token_is_never_logged_in_production(
    app, admin_client, session, caplog
):
    from models import User
    from services import accounts

    app.config["ENV_NAME"] = "production"
    app.config["NOTIFY_SMTP_HOST"] = None

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    token = accounts.issue_token(user, accounts.EMAIL_VERIFICATION)

    with caplog.at_level("DEBUG"):
        accounts.deliver_verification_link(user, token)

    assert token not in caplog.text


def test_reset_link_is_still_logged_in_development(
    app, admin_client, session, caplog
):
    """A developer with no SMTP configured must still be able to reset."""
    from models import User
    from services import accounts

    app.config["ENV_NAME"] = "development"
    app.config["NOTIFY_SMTP_HOST"] = None

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    token = accounts.issue_token(user, accounts.PASSWORD_RESET)

    with caplog.at_level("WARNING"):
        accounts.deliver_reset_link(user, token)

    assert token in caplog.text


# ======================================================================
# F9 - a read-only Viewer could trigger a monitoring cycle with a GET
# ======================================================================


def test_viewer_cannot_trigger_a_monitoring_cycle(app, viewer_client, make_application):
    from unittest.mock import patch

    from services import health as health_service

    make_application()
    calls = []
    original = health_service.run_monitoring_cycle

    def spy(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    with patch("services.health.run_monitoring_cycle", side_effect=spy):
        response = viewer_client.get("/api/monitor", headers=JSON_HEADERS)

    assert response.status_code == 403
    assert calls == []


def test_viewer_still_cannot_post_to_run(app, viewer_client, make_application):
    make_application()
    response = viewer_client.post("/api/monitor/run", headers=JSON_HEADERS)
    assert response.status_code == 403


def test_admin_can_still_trigger_a_monitoring_cycle(admin_client, make_application):
    from unittest.mock import patch

    make_application()
    calls = []
    original = health_service_run()

    def spy(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    with patch("services.health.run_monitoring_cycle", side_effect=spy):
        response = admin_client.get("/api/monitor", headers=JSON_HEADERS)

    assert response.status_code == 200
    assert calls == [True]


def health_service_run():
    from services import health as health_service

    return health_service.run_monitoring_cycle


# ======================================================================
# F12 - schema backfill wrote local time into a UTC column
# ======================================================================


def test_backfill_literal_uses_utc_not_local_time():
    from datetime import UTC, datetime

    from services.schema import _backfill_literal

    class FakeColumn:
        # _backfill_literal inspects column.type.python_type and checks
        # issubclass against datetime.datetime.
        class _Type:
            python_type = datetime

        type = _Type()

    literal = _backfill_literal(FakeColumn())

    written = datetime.fromisoformat(literal.strip("'")).replace(tzinfo=UTC)
    now = datetime.now(UTC)

    # A local-time backfill would be skewed by the host's UTC offset. On this
    # machine that is 5.5 hours, far more than this tolerance.
    assert abs((now - written).total_seconds()) < 60


# ======================================================================
# F14 - activity id filters vanished on 0 or on garbage
# ======================================================================


def test_entity_id_zero_filters_instead_of_returning_everything(admin_client):
    from services import audit

    audit.record("thing.created", entity_type="thing", entity_id=1, summary="one")
    audit.record("thing.created", entity_type="thing", entity_id=2, summary="two")
    audit.record("thing.created", entity_type="thing", entity_id=0, summary="zero")

    response = admin_client.get("/api/activity?entity_id=0", headers=JSON_HEADERS)
    entries = response.get_json()

    assert [entry["summary"] for entry in entries] == ["zero"]


def test_a_non_numeric_entity_id_is_a_client_error(admin_client):
    response = admin_client.get("/api/activity?entity_id=abc", headers=JSON_HEADERS)
    assert response.status_code == 422


def test_a_missing_entity_id_filter_returns_everything(admin_client):
    from services import audit

    audit.record("thing.created", entity_type="thing", entity_id=1, summary="one")
    audit.record("thing.created", entity_type="thing", entity_id=2, summary="two")

    response = admin_client.get("/api/activity", headers=JSON_HEADERS)
    assert len(response.get_json()) == 2


# ======================================================================
# F16 - registration accepted passwords the reset flow would reject
# ======================================================================


def test_registration_and_reset_enforce_the_same_policy(app, client):
    from services import accounts

    weak = "aaaaaaaa"
    assert accounts.password_strength_errors(weak), "the policy should reject this"

    response = client.post(
        "/register",
        data={"username": "weak", "email": "weak@cloudpulse.test", "password": weak},
    )
    assert response.status_code == 200
    assert b"lowercase, uppercase" in response.data


# ======================================================================
# F17 - unknown addresses were not rate limited on /forgot-password
# ======================================================================


def test_unknown_addresses_are_throttled_too(app, client):
    """The limiter used to engage only for addresses that exist, which is
    exactly the set an attacker does not have."""
    from services import ratelimit

    ratelimit.reset()
    app.config["PASSWORD_RESET_MAX_ATTEMPTS"] = 3
    app.config["STRICT_ORIGIN_CHECK"] = False

    for _ in range(5):
        client.post("/forgot-password", data={"email": "invented@cloudpulse.test"})

    assert (
        ratelimit.failure_count("password_reset", "invented@cloudpulse.test", PEER_IP)
        >= 3
    )


# ======================================================================
# MINOR - "Try again in 0 seconds"
# ======================================================================


def test_retry_after_is_never_zero_while_throttled(app):
    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_MAX_ATTEMPTS"] = 1
    app.config["LOGIN_ATTEMPT_WINDOW_SECONDS"] = 300

    ratelimit.record_failure("login", "user@cloudpulse.test", "1.1.1.1")

    assert ratelimit.retry_after_seconds("login", "user@cloudpulse.test", "1.1.1.1") >= 1


def test_throttled_registration_shows_a_useful_wait(app, client):
    """A refused registration must not say "retry in 0 seconds".

    A successful registration returns its budget, so the budget is consumed by
    attempts that do not succeed: conflicts, which never create an account.
    """
    from services import ratelimit

    ratelimit.reset()
    app.config["REGISTRATION_MAX_ATTEMPTS"] = 2
    app.config["STRICT_ORIGIN_CHECK"] = False

    page = client.get("/register")
    token = csrf_token_from(page)

    # Register the address, then keep conflicting with it. Each conflict is a
    # real attempt against the same (email, IP) bucket.
    client.post(
        "/register",
        data={
            "username": "owner",
            "email": "contested@cloudpulse.test",
            "password": "Str0ng!Passw0rd",
            "csrf_token": token,
        },
    )

    for index in range(2):
        response = client.post(
            "/register",
            data={
                "username": f"impostor{index}",
                "email": "contested@cloudpulse.test",
                "password": "Str0ng!Passw0rd",
                "csrf_token": token,
            },
        )
        assert response.status_code == 200
        assert b"already registered" in response.data

    blocked = client.post(
        "/register",
        data={
            "username": "impostor-final",
            "email": "contested@cloudpulse.test",
            "password": "Str0ng!Passw0rd",
            "csrf_token": token,
        },
    )

    assert blocked.status_code == 429
    assert b"Try again in 0 seconds" not in blocked.data
    assert b"Too many attempts" in blocked.data


# ======================================================================
# A published placeholder key must not boot production
#
# The kustomize overlays and the chart ship
# SECRET_KEY=replace-me-with-a-long-random-value. That value is in a public
# repository, so accepting it in production means the signing key is readable
# by anyone, which is worse than having no key because it looks configured.
# ======================================================================


@pytest.mark.parametrize(
    "key",
    [
        "replace-me-with-a-long-random-value",
        "REPLACE-ME-WITH-A-LONG-RANDOM-VALUE",
        "changeme",
        "change-me",
        "your-secret-key-here",
        "cloudpulse-development-secret-key",
    ],
)
def test_a_published_placeholder_key_is_refused(key, monkeypatch):
    import config as config_module

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", key)
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="placeholder published"):
        config_module.validate_config(
            "production", {"SESSION_COOKIE_SECURE": True, "STRICT_ORIGIN_CHECK": True}
        )


def test_a_short_key_is_refused(monkeypatch):
    import config as config_module

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "tooshort")
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        config_module.validate_config(
            "production", {"SESSION_COOKIE_SECURE": True, "STRICT_ORIGIN_CHECK": True}
        )


def test_a_real_key_is_accepted(monkeypatch):
    import config as config_module

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "k" * 48)
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)

    config_module.validate_config(
        "production", {"SESSION_COOKIE_SECURE": True, "STRICT_ORIGIN_CHECK": True}
    )


def test_the_escape_hatch_still_works(monkeypatch):
    """Deliberately insecure deployments must remain possible."""
    import config as config_module

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "replace-me-with-a-long-random-value")
    monkeypatch.setenv("ALLOW_INSECURE_SECRET_KEY", "true")

    config_module.validate_config(
        "production", {"SESSION_COOKIE_SECURE": True, "STRICT_ORIGIN_CHECK": True}
    )


def test_the_overlay_placeholder_is_covered():
    """The value the overlay actually ships must be one of the refused ones.

    Otherwise the check above passes while the thing an operator would actually
    paste into production is still accepted.
    """
    import pathlib

    import config as config_module

    overlay = pathlib.Path("k8s/overlays/production/kustomization.yaml").read_text(
        encoding="utf-8"
    )
    match = re.search(r"SECRET_KEY=(\S+)", overlay)
    assert match, "SECRET_KEY not found in the production overlay"

    assert config_module._looks_like_placeholder_key(match.group(1)), (
        f"the overlay ships SECRET_KEY={match.group(1)!r}, which production would "
        "accept. Either change the placeholder or add it to "
        "INSECURE_PLACEHOLDER_KEYS."
    )


# ======================================================================
# F18 - a reset token could be spent twice concurrently
#
# consume_token() checked used_at and only wrote it later, after the password
# hash. Two parallel requests both passed the check and the second silently
# overwrote the first's password.
# ======================================================================


def test_claim_token_marks_the_token_used(app, admin_client, session):
    from models import User
    from services import accounts

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    raw = accounts.issue_token(user, accounts.PASSWORD_RESET)

    assert accounts.claim_token(raw, accounts.PASSWORD_RESET) is not None
    # A second claim finds nothing, because the first one spent it.
    assert accounts.claim_token(raw, accounts.PASSWORD_RESET) is None


def test_claim_token_refuses_an_expired_token(app, admin_client, session):
    from datetime import UTC, datetime, timedelta

    from models import User
    from services import accounts

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    raw = accounts.issue_token(user, accounts.PASSWORD_RESET)

    from models import AuthToken

    token = session.query(AuthToken).filter_by(
        token_hash=accounts.hash_token(raw)
    ).one()
    token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    assert accounts.claim_token(raw, accounts.PASSWORD_RESET) is None


def test_claim_token_refuses_the_wrong_purpose(app, admin_client, session):
    from models import User
    from services import accounts

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    raw = accounts.issue_token(user, accounts.PASSWORD_RESET)

    assert accounts.claim_token(raw, accounts.EMAIL_VERIFICATION) is None
    # The wrong purpose must not have consumed it.
    assert accounts.claim_token(raw, accounts.PASSWORD_RESET) is not None


def test_only_one_of_two_concurrent_resets_succeeds(app, admin_client, session):
    """The exact race: two requests spending one token at the same time.

    Driven on a file-backed database. The suite's in-memory SQLite gives each
    thread its own connection, so both statements would fail outright rather
    than interleave, and the test would pass for the wrong reason.
    """
    import tempfile
    from pathlib import Path

    from extensions import db as _db
    from models import AuthToken, User
    from services import accounts
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import QueuePool
    from werkzeug.security import generate_password_hash

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        # A real pooled file database, not StaticPool. StaticPool hands every
        # session the same connection, so the two "concurrent" workers would
        # share one transaction and could never genuinely interleave.
        # check_same_thread=False because the pooled connection is created on
        # the main thread and reused by the workers.
        engine = create_engine(
            f"sqlite:///{Path(directory) / 'race.db'}",
            connect_args={"check_same_thread": False, "timeout": 15},
            poolclass=QueuePool,
        )
        _db.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, future=True)

        setup = factory()
        racer = User(
            username="racer",
            email="racer@cloudpulse.test",
            password_hash=generate_password_hash(ADMIN_PASSWORD),
            role="Viewer",
        )
        setup.add(racer)
        setup.commit()

        raw = accounts.issue_token_in_session(
            setup, accounts.PASSWORD_RESET, user_id=racer.id
        )
        setup.close()

        results: list[bool] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def attempt(password: str) -> None:
            worker = factory()
            try:
                barrier.wait(timeout=10)
                outcome = accounts.reset_password_in_session(
                    worker, raw, password, password
                )
                with results_lock:
                    results.append(bool(outcome.get("ok")))
            except Exception as error:  # pragma: no cover - diagnostics
                with results_lock:
                    errors.append(error)
            finally:
                worker.close()

        threads = [
            threading.Thread(target=attempt, args=("First!Password9",)),
            threading.Thread(target=attempt, args=("Second!Password9",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        check = factory()
        try:
            # expire_all before reading: the session may still hold the
            # identity-mapped row from before the workers claimed it.
            check.expire_all()
            remaining = (
                check.query(AuthToken)
                .filter_by(token_hash=accounts.hash_token(raw))
                .one()
                .used_at
            )
        finally:
            check.close()
            engine.dispose()

    assert not errors, f"unexpected exceptions: {errors}"
    assert sum(results) == 1, f"expected exactly one winner, got {results}"
    assert remaining is not None, "the token must end up marked used"


def test_consume_token_does_not_spend_the_token(app, admin_client, session):
    """consume_token is a read; claiming is a separate, explicit step."""
    from models import User
    from services import accounts

    user = session.query(User).filter_by(email=ADMIN_EMAIL).one()
    raw = accounts.issue_token(user, accounts.PASSWORD_RESET)

    assert accounts.consume_token(raw, accounts.PASSWORD_RESET) is not None
    assert accounts.consume_token(raw, accounts.PASSWORD_RESET) is not None

    result = accounts.reset_password(raw, "Reset!Password9", "Reset!Password9")
    assert result["ok"] is True


def test_email_verification_token_is_also_single_use(app, client, session):
    from models import User
    from services import accounts

    client.post(
        "/register",
        data={
            "username": "atomicverify",
            "email": "atomicverify@cloudpulse.test",
            "password": "Str0ng!Passw0rd",
        },
    )
    user = session.query(User).filter_by(email="atomicverify@cloudpulse.test").one()
    raw = accounts.issue_token(user, accounts.EMAIL_VERIFICATION)

    assert accounts.verify_email(raw)["ok"] is True
    assert accounts.verify_email(raw)["ok"] is False


# ======================================================================
# NIT - identifiers containing the old key separator
# ======================================================================


def test_identifier_containing_a_pipe_is_parsed_correctly(app):
    from services import ratelimit

    ratelimit.reset()
    ratelimit.record_failure("login", "we|ird@cloudpulse.test", "1.1.1.1")

    scope, identifier, client_ip = ratelimit._split(
        ratelimit._key("login", "we|ird@cloudpulse.test", "1.1.1.1")
    )
    assert (scope, identifier, client_ip) == (
        "login",
        "we|ird@cloudpulse.test",
        "1.1.1.1",
    )


def test_purge_uses_the_right_window_for_a_pipe_identifier(app):
    import time

    from services import ratelimit

    ratelimit.reset()
    app.config["LOGIN_ATTEMPT_WINDOW_SECONDS"] = 1
    app.config["REGISTRATION_MAX_ATTEMPTS"] = 900

    ratelimit.record_failure("login", "we|ird@cloudpulse.test", "1.1.1.1")
    time.sleep(1.1)

    # A mis-parsed key would have used the 900-second window and kept the row.
    assert ratelimit.purge_expired() >= 1


# ======================================================================
# NIT - the JSON formatter's inherited converter would have raised
# ======================================================================


def test_json_formatter_stamps_utc_in_both_paths():
    """The two timestamp paths must not disagree.

    format() writes an ISO-8601 UTC stamp; formatTime is overridden to match
    rather than fall back to the inherited local-time rendering. The inherited
    method is not broken, so this pins consistency rather than a crash.
    """
    import logging
    from datetime import UTC, datetime

    from services.logging_config import JSONFormatter

    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="m", args=(), exc_info=None,
    )
    formatter = JSONFormatter()

    stamped = formatter.formatTime(record)
    assert stamped.endswith("+00:00"), stamped
    assert json.loads(formatter.format(record))["timestamp"].startswith(
        datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%d")
    )


def csrf_token_from(response):
    import re

    match = re.search(r'name="csrf_token" value="([^"]+)"', response.data.decode())
    return match.group(1) if match else ""


def test_admin_fixture_password_satisfies_the_policy(app):
    """Guards the fixture itself: a weak password here silently breaks
    every test that registers through the real endpoint."""
    from services import accounts

    assert accounts.password_strength_errors(ADMIN_PASSWORD) == []
