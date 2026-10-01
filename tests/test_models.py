"""Model, serializer and configuration unit tests."""

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------
# Status code expansion
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "spec,expected_members",
    [
        ("200", {200}),
        ("200-202", {200, 201, 202}),
        ("200-399,404", set(range(200, 400)) | {404}),
        ("", set(range(200, 400))),
        (None, set(range(200, 400))),
        ("garbage", set(range(200, 400))),
        ("202-200", {200, 201, 202}),
    ],
)
def test_expand_status_codes(spec, expected_members):
    from models import expand_status_codes

    assert expand_status_codes(spec) == expected_members


def test_application_accepted_status_codes(app, make_application):
    application = make_application(expected_status_codes="200,204")
    assert application.accepted_status_codes == {200, 204}


def test_application_parsed_headers(app, make_application):
    application = make_application(request_headers='{"Authorization": "Bearer t"}')
    assert application.parsed_request_headers == {"Authorization": "Bearer t"}


def test_application_parsed_headers_ignores_invalid_json(app, make_application):
    application = make_application(request_headers="not-json")
    assert application.parsed_request_headers == {}


def test_application_parsed_headers_ignores_non_object(app, make_application):
    application = make_application(request_headers="[1, 2, 3]")
    assert application.parsed_request_headers == {}


# ---------------------------------------------------------------------
# Check constraints
# ---------------------------------------------------------------------

def test_invalid_severity_is_rejected_by_database(app, session, make_application):
    from models import Incident
    from sqlalchemy.exc import IntegrityError

    application = make_application()
    session.add(Incident(application=application, title="bad", severity="Apocalyptic"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_invalid_incident_status_is_rejected_by_database(app, session, make_application):
    from models import Incident
    from sqlalchemy.exc import IntegrityError

    application = make_application()
    session.add(Incident(application=application, title="bad", status="Maybe"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_invalid_deployment_status_is_rejected_by_database(app, session, make_application):
    from models import Deployment
    from sqlalchemy.exc import IntegrityError

    application = make_application()
    session.add(
        Deployment(
            application=application,
            version="v1",
            environment="Production",
            status="Sort of",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_timestamps_are_populated(app, session, make_application):
    application = make_application()
    assert application.created_at is not None
    assert application.created_at.year == datetime.now(UTC).year


# ---------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------

def test_incident_serializer_keeps_legacy_keys(app, make_incident):
    from serializers import incident_to_dict

    payload = incident_to_dict(make_incident())
    for key in (
        "id",
        "application_id",
        "application_name",
        "title",
        "description",
        "severity",
        "status",
        "created_at",
        "resolved_at",
    ):
        assert key in payload


def test_application_serializer_keeps_legacy_keys(app, make_application):
    from serializers import application_to_dict

    payload = application_to_dict(make_application())
    for key in ("id", "name", "url", "environment", "status", "description", "created_at"):
        assert key in payload


def test_deployment_serializer_keeps_legacy_keys(app, make_deployment):
    from serializers import deployment_to_dict

    payload = deployment_to_dict(make_deployment())
    for key in ("id", "application_id", "application_name", "version", "environment",
                "status", "deployed_at"):
        assert key in payload


def test_health_check_serializer(app, session, make_application):
    from models import HealthCheck
    from serializers import health_check_to_dict

    application = make_application()
    check = HealthCheck(
        application_id=application.id,
        status="UP",
        http_status=200,
        response_time=10.5,
    )
    session.add(check)
    session.commit()

    payload = health_check_to_dict(check)
    assert payload["status"] == "UP"
    assert payload["response_time"] == 10.5
    assert payload["checked_at"] is not None


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

def test_database_url_normalisation():
    from config import normalize_database_url

    assert normalize_database_url("postgres://u:p@h/db").startswith("postgresql://")
    assert normalize_database_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_env_helpers(monkeypatch):
    from config import env_bool, env_int, env_list

    monkeypatch.setenv("CP_TEST_FLAG", "TRUE")
    monkeypatch.setenv("CP_TEST_NUMBER", "42")
    monkeypatch.setenv("CP_TEST_LIST", "a, b ,c")

    assert env_bool("CP_TEST_FLAG") is True
    assert env_int("CP_TEST_NUMBER", 0) == 42
    assert env_list("CP_TEST_LIST") == ["a", "b", "c"]


def test_env_helpers_fall_back_on_bad_input(monkeypatch):
    from config import env_bool, env_float, env_int

    monkeypatch.setenv("CP_TEST_BAD", "not-a-number")
    monkeypatch.setenv("CP_TEST_FLAG", "maybe")

    assert env_int("CP_TEST_BAD", 7) == 7
    assert env_float("CP_TEST_BAD", 1.5) == 1.5
    assert env_bool("CP_TEST_FLAG", True) is True


def test_testing_config_uses_in_memory_database():
    from config import get_config

    config = get_config("testing")
    assert config.SQLALCHEMY_DATABASE_URI == "sqlite://"
    assert config.TESTING is True
    assert config.ENABLE_SCHEDULER is False
