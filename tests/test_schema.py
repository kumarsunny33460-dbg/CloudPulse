"""Schema synchronisation tests.

The important guarantee is that an application upgrade works against a database
created by an *older* version of the models, without losing data and without
requiring a manual rebuild.
"""

import pytest
from conftest import APP_DIR  # noqa: F401  (ensures the app import path is set)
from sqlalchemy import inspect, text

pytestmark = pytest.mark.integration


def _drop_table(app, table_name: str) -> None:
    with app.app_context():
        from extensions import db

        db.session.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
        db.session.commit()


def _drop_column(app, table_name: str, column_name: str) -> None:
    with app.app_context():
        from extensions import db

        db.session.execute(
            text(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"')
        )
        db.session.commit()


def _columns(app, table_name: str) -> set[str]:
    with app.app_context():
        inspector = inspect(db_engine())
        return {column["name"] for column in inspector.get_columns(table_name)}


def db_engine():
    from extensions import db

    return db.engine


def test_sync_recreates_a_dropped_table(app, client):
    from services import schema

    _drop_table(app, "health_checks")
    assert "health_checks" not in _table_names(app)

    result = schema.sync_schema()

    assert result is not None
    assert "health_checks" in _table_names(app)


def _table_names(app) -> set[str]:
    with app.app_context():
        return set(inspect(db_engine()).get_table_names())


def test_sync_adds_a_missing_column(app, client):
    from services import schema

    _drop_column(app, "applications", "expected_status_codes")
    assert "expected_status_codes" not in _columns(app, "applications")

    result = schema.sync_schema()

    assert "applications.expected_status_codes" in result["columns_added"]
    assert "expected_status_codes" in _columns(app, "applications")


def test_sync_adds_a_missing_not_null_datetime_column(app, client):
    from services import schema

    _drop_column(app, "incidents", "detected_at")
    assert "detected_at" not in _columns(app, "incidents")

    result = schema.sync_schema()

    assert "incidents.detected_at" in result["columns_added"]
    assert "detected_at" in _columns(app, "incidents")


def test_sync_preserves_existing_rows(app, admin_client, make_application):
    from services import schema

    application = make_application(name="survivor", environment="Staging")

    _drop_column(app, "applications", "uptime_percentage")
    schema.sync_schema()

    from extensions import db
    from models import Application

    reloaded = db.session.get(Application, application.id)
    assert reloaded is not None
    assert reloaded.name == "survivor"
    assert reloaded.environment == "Staging"


def test_sync_is_idempotent(app, client):
    from services import schema

    schema.sync_schema()
    second = schema.sync_schema()

    assert second["columns_added"] == []


def test_sync_creates_indexes(app, client):
    from services import schema

    schema.sync_schema()

    with app.app_context():
        inspector = inspect(db_engine())
        index_names = {
            index["name"]
            for table in inspector.get_table_names()
            for index in inspector.get_indexes(table)
        }

    assert "ix_health_checks_application_checked" in index_names
    assert "ix_incidents_application_status" in index_names
    assert "ix_audit_entity" in index_names


def test_application_is_usable_after_sync(admin_client, make_application, app):
    """The API must work end to end against an upgraded database."""
    from services import schema

    # Columns referenced by a CHECK constraint cannot be dropped by SQLite,
    # so use the free-form monitoring columns to simulate the older schema.
    _drop_column(app, "applications", "expected_body_keyword")
    _drop_column(app, "applications", "last_response_time")
    schema.sync_schema()

    make_application(name="post-upgrade")

    response = admin_client.get("/api/applications", headers={"X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200
    assert response.get_json()[0]["name"] == "post-upgrade"


def test_sync_adds_columns_to_a_populated_table(app, admin_client, make_application):
    """A NOT NULL column must be addable when the table already has rows."""
    from services import schema

    for index in range(3):
        make_application(name=f"row-{index}")

    _drop_column(app, "incidents", "acknowledged_at")
    _drop_column(app, "applications", "consecutive_failures")

    schema.sync_schema()

    with app.app_context():
        from extensions import db

        rows = db.session.execute(text("SELECT consecutive_failures FROM applications")).all()
        assert len(rows) == 3
        assert all(row[0] == 0 for row in rows)
