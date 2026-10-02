"""A hosted provider's DATABASE_URL must work whichever SQLAlchemy it installs.

Render hands out a bare ``postgresql://`` connection string. Left alone, the
driver is SQLAlchemy's default, which is not stable across versions: 2.0
resolves it to ``psycopg2`` and 2.1 resolves it to ``psycopg`` (v3). Only
``psycopg2-binary`` is in requirements.txt, so the build that picked up the
newer SQLAlchemy failed at boot with

    ModuleNotFoundError: No module named 'psycopg'

which is what the first Render deploy of this project did.
"""

from __future__ import annotations

from config import normalize_database_url
from sqlalchemy.engine import make_url


def test_a_legacy_postgres_scheme_is_rewritten():
    assert normalize_database_url("postgres://u:p@host/db") == (
        "postgresql+psycopg2://u:p@host/db"
    )


def test_a_bare_postgresql_url_gets_the_installed_driver_pinned():
    """The Render shape. This is the one that broke."""
    normalised = normalize_database_url("postgresql://cloudpulse:secret@db:5432/cloudpulse")

    assert normalised == "postgresql+psycopg2://cloudpulse:secret@db:5432/cloudpulse"
    assert make_url(normalised).drivername == "postgresql+psycopg2"


def test_the_driver_is_importable():
    """Pinning a driver that is not installed would just move the crash."""
    import importlib.util

    assert importlib.util.find_spec("psycopg2") is not None, (
        "the URL is pinned to psycopg2, so psycopg2-binary must be installed"
    )


def test_an_explicit_driver_is_respected():
    """An operator who names a driver has already made the choice."""
    for url in (
        "postgresql+psycopg2://u:p@h/db",
        "postgresql+psycopg://u:p@h/db",
        "postgresql+asyncpg://u:p@h/db",
    ):
        assert normalize_database_url(url) == url


def test_credentials_and_host_survive_normalisation():
    original = "postgres://user:s3cr3t@internal-host:5432/cloudpulse?sslmode=require"
    normalised = normalize_database_url(original)

    assert "user:s3cr3t@internal-host:5432" in normalised
    assert "sslmode=require" in normalised


def test_a_non_postgres_url_is_untouched():
    for url in ("sqlite:///instance/cloudpulse.db", "mysql://u:p@h/db"):
        assert normalize_database_url(url) == url


def test_query_parameters_are_preserved():
    normalised = normalize_database_url(
        "postgresql://u:p@host:5432/db?sslmode=require&connect_timeout=10"
    )

    assert normalised.startswith("postgresql+psycopg2://u:p@host:5432/db")
    assert "sslmode=require" in normalised
    assert "connect_timeout=10" in normalised


def test_the_url_sqlalchemy_loads_names_the_driver_it_will_use():
    """End to end: what SQLAlchemy resolves the normalised URL to."""
    normalised = normalize_database_url("postgres://u:p@host/db")
    parsed = make_url(normalised)

    assert parsed.drivername == "postgresql+psycopg2"
    assert parsed.database == "db"
    assert parsed.username == "u"
