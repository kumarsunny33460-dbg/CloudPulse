"""Shared pytest fixtures for the CloudPulse test suite.

Every test runs against an isolated in-memory SQLite database created through
the application factory, so the suite never touches the developer database.
"""

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from extensions import db as _db  # noqa: E402

from app import create_app  # noqa: E402

JSON_HEADERS = {"X-Requested-With": "XMLHttpRequest"}

ADMIN_EMAIL = "admin@cloudpulse.test"
# Registration and password reset apply the same policy, so the fixture
# passwords must satisfy it. A weak one would be rejected by /register and the
# account would never be created.
ADMIN_PASSWORD = "Adm1n-Secret!"
VIEWER_EMAIL = "viewer@cloudpulse.test"
VIEWER_PASSWORD = "V1ewer-Secret!"

# The peer address the test client reports. X-Forwarded-For is only believed
# from a configured trusted proxy, so this is the address rate limits key on.
TEST_CLIENT_IP = "198.51.100.7"


@pytest.fixture
def app():
    application = create_app("testing")

    with application.app_context():
        _db.drop_all()
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    # A client address is set explicitly. Without it the rate limit keys bucket
    # every request under "unknown", which hides whether a test exercised the
    # per-client scoping at all.
    #
    # FlaskClient does not accept environ_base, so the peer is set through the
    # default environ the app configures, which audit.client_ip() reads.
    app.config["SERVER_NAME"] = app.config.get("SERVER_NAME") or "localhost"
    test_client = app.test_client()
    test_client.environ_base["REMOTE_ADDR"] = TEST_CLIENT_IP
    return test_client


@pytest.fixture
def session(app):
    return _db.session


def _register(client, username, email, password):
    return client.post(
        "/register",
        data={"username": username, "email": email, "password": password},
        follow_redirects=False,
    )


def _login(client, email, password):
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


@pytest.fixture
def admin_client(client):
    _register(client, "admin", ADMIN_EMAIL, ADMIN_PASSWORD)
    _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    return client


@pytest.fixture
def viewer_client(client):
    _register(client, "admin", ADMIN_EMAIL, ADMIN_PASSWORD)
    _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    with client.session_transaction() as session:
        session.clear()

    _register(client, "viewer", VIEWER_EMAIL, VIEWER_PASSWORD)
    _login(client, VIEWER_EMAIL, VIEWER_PASSWORD)
    return client


@pytest.fixture
def make_application(session):
    from models import Application

    def factory(**overrides):
        values = {
            "name": "test-service",
            "url": "https://example.com/health",
            "environment": "Production",
            "status": "Operational",
            "description": "Fixture application",
        }
        values.update(overrides)
        application = Application(**values)
        session.add(application)
        session.commit()
        return application

    return factory


@pytest.fixture
def make_incident(session, make_application):
    from models import Incident

    def factory(**overrides):
        application = overrides.pop("application", None) or make_application()
        values = {
            "application": application,
            "title": "Fixture incident",
            "description": "Something went wrong",
            "severity": "High",
            "status": "Open",
            "source": "manual",
        }
        values.update(overrides)
        incident = Incident(**values)
        session.add(incident)
        session.commit()
        return incident

    return factory


@pytest.fixture
def make_deployment(session, make_application):
    from models import Deployment

    def factory(**overrides):
        application = overrides.pop("application", None) or make_application()
        values = {
            "application": application,
            "version": "v1.0.0",
            "environment": "Production",
            "status": "Successful",
        }
        values.update(overrides)
        deployment = Deployment(**values)
        session.add(deployment)
        session.commit()
        return deployment

    return factory
