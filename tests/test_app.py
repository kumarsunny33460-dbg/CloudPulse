import sys
from pathlib import Path

import pytest

# Add the app directory to Python's import path
APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True

    with app.test_client() as client:
        yield client


def test_login_page(client):
    response = client.get("/login")

    assert response.status_code == 200


def test_register_page(client):
    response = client.get("/register")

    assert response.status_code == 200


def test_unauthenticated_api_access(client):
    response = client.get("/api/me")

    assert response.status_code == 401


def test_home_requires_login(client):
    response = client.get("/")

    assert response.status_code in (302, 401)