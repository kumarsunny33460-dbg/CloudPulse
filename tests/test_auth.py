"""Authentication and role based authorisation tests.

The authentication flow itself is unchanged; these tests lock in the existing
behaviour and verify the new ``write_required`` authorisation layer.
"""

import pytest
from conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    JSON_HEADERS,
)

pytestmark = pytest.mark.integration


def test_unauthenticated_api_access_is_rejected(client):
    response = client.get("/api/applications")
    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_first_user_becomes_admin(client):
    client.post(
        "/register",
        data={"username": "first", "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    client.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})

    payload = client.get("/api/me", headers=JSON_HEADERS).get_json()
    assert payload["user"]["role"] == "Admin"


def test_second_user_is_viewer(viewer_client):
    payload = viewer_client.get("/api/me", headers=JSON_HEADERS).get_json()
    assert payload["user"]["role"] == "Viewer"


def test_duplicate_email_is_rejected(client):
    client.post(
        "/register",
        data={"username": "first", "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    response = client.post(
        "/register",
        data={"username": "second", "email": ADMIN_EMAIL, "password": "Another!Secret9"},
    )
    assert response.status_code == 200
    assert b"already registered" in response.data


def test_short_password_is_rejected(client):
    response = client.post(
        "/register",
        data={"username": "shorty", "email": "short@example.com", "password": "123"},
    )
    assert response.status_code == 200
    assert b"at least 8 characters" in response.data


def test_weak_password_is_rejected_by_the_shared_policy(client):
    """Registration and password reset must enforce the same rules."""
    response = client.post(
        "/register",
        data={"username": "weak", "email": "weak@example.com", "password": "aaaaaaaa"},
    )
    assert response.status_code == 200
    assert b"lowercase, uppercase" in response.data


def test_invalid_credentials_are_rejected(client):
    client.post(
        "/register",
        data={"username": "first", "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    response = client.post(
        "/login", data={"email": ADMIN_EMAIL, "password": "wrong-password"}
    )
    assert b"Invalid email or password" in response.data


def test_viewer_cannot_create_applications(viewer_client):
    response = viewer_client.post(
        "/api/applications",
        json={"name": "blocked", "url": "https://example.com", "environment": "Production"},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 403
    assert response.get_json()["error"] == "Insufficient permissions for this action"


def test_viewer_can_read_applications(viewer_client):
    response = viewer_client.get("/api/applications", headers=JSON_HEADERS)
    assert response.status_code == 200
    assert response.get_json() == []


def test_viewer_cannot_delete_applications(viewer_client, make_application):
    application = make_application()
    response = viewer_client.delete(
        "/api/applications/" + str(application.id), headers=JSON_HEADERS
    )
    assert response.status_code == 403


def test_editor_role_can_write(client):
    """An Editor promoted directly in the database may mutate data."""
    from extensions import db
    from models import User

    client.post(
        "/register",
        data={"username": "root", "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    client.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})

    user = db.session.query(User).filter_by(email=ADMIN_EMAIL).first()
    user.role = "Editor"
    db.session.commit()

    with client.session_transaction() as session:
        session["role"] = "Editor"

    response = client.post(
        "/api/applications",
        json={"name": "editor-app", "url": "https://example.com", "environment": "Staging"},
        headers=JSON_HEADERS,
    )
    assert response.status_code == 201


def test_cross_origin_mutation_is_blocked(admin_client, app):
    app.config["CSRF_PROTECTION_ENABLED"] = True
    app.config["STRICT_ORIGIN_CHECK"] = True

    response = admin_client.post(
        "/api/applications",
        json={"name": "csrf", "url": "https://example.com", "environment": "Production"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403
    assert "Cross-origin" in response.get_json()["error"]


def test_same_origin_mutation_is_allowed(admin_client, app):
    app.config["CSRF_PROTECTION_ENABLED"] = True
    app.config["STRICT_ORIGIN_CHECK"] = True

    response = admin_client.post(
        "/api/applications",
        json={"name": "ok-app", "url": "https://example.com", "environment": "Production"},
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 201
