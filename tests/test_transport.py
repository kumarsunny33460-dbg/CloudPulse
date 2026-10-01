"""Health check transport behaviour: TLS options and failure messages."""

import socket
import ssl
from unittest.mock import MagicMock, patch

import pytest
from conftest import JSON_HEADERS
from services.health import probe

pytestmark = pytest.mark.integration


def test_tls_verification_is_on_by_default(app):
    from services.health import _ssl_context

    with app.app_context():
        assert _ssl_context() is None


def test_tls_verification_can_be_disabled(app):
    from services.health import _ssl_context

    app.config["HEALTH_CHECK_VERIFY_TLS"] = False
    with app.app_context():
        context = _ssl_context()

    assert context is not None
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


def test_ssl_context_is_passed_to_urlopen(app, make_application):
    from services.health import probe

    app.config["HEALTH_CHECK_VERIFY_TLS"] = False
    application = make_application(url="https://example.com/health")

    captured = {}

    def fake_urlopen(request, timeout=None, context=None):
        captured["context"] = context
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.status = 200
        response.read = MagicMock(return_value=b"")
        return response

    with patch("services.health.urlopen", side_effect=fake_urlopen):
        result = probe(application)

    assert result["health"] == "UP"
    assert captured["context"] is not None


def test_certificate_error_produces_an_actionable_message(app, make_application):
    from services.health import probe

    application = make_application(url="https://proxied.example.com/health")

    certificate_error = ssl.SSLCertVerificationError(
        "certificate verify failed: self-signed certificate in certificate chain"
    )
    with patch("services.health.urlopen", side_effect=URLError_factory(certificate_error)):
        result = probe(application)

    assert result["health"] == "DOWN"
    assert "TLS certificate verification failed" in result["message"]
    assert "HEALTH_CHECK_VERIFY_TLS" in result["message"]


def test_dns_failure_is_described(app, make_application):
    from services.health import probe

    application = make_application(url="https://does-not-resolve.invalid/health")

    with patch("services.health.urlopen", side_effect=URLError_factory(socket.gaierror(-2, "Name or service not known"))):
        result = probe(application)

    assert result["health"] == "DOWN"
    assert "DNS lookup failed" in result["message"]


def test_connection_refused_is_described(app, make_application):
    from services.health import probe

    application = make_application(url="https://127.0.0.1:9/health")

    with patch("services.health.urlopen", side_effect=URLError_factory(ConnectionRefusedError(111, "Connection refused"))):
        result = probe(application)

    assert "Connection refused" in result["message"]


def test_timeout_is_described(app, make_application):
    from services.health import probe

    application = make_application(url="https://slow.example.com/health")

    with patch("services.health.urlopen", side_effect=URLError_factory(TimeoutError("timed out"))):
        result = probe(application)

    assert "Timed out" in result["message"]


def test_generic_failure_still_reports_unreachable(app, make_application):
    from services.health import probe

    application = make_application(url="https://weird.example.com/health")

    with patch("services.health.urlopen", side_effect=URLError_factory(OSError("something odd"))):
        result = probe(application)

    assert "unreachable" in result["message"]


def test_manual_check_returns_the_descriptive_message(admin_client, make_application):
    application = make_application(url="https://proxied.example.com/health")

    certificate_error = ssl.SSLCertVerificationError("certificate verify failed")
    with patch("services.health.urlopen", side_effect=URLError_factory(certificate_error)):
        response = admin_client.post(
            "/api/applications/" + str(application.id) + "/check", headers=JSON_HEADERS
        )

    assert response.status_code == 200
    assert "TLS certificate" in response.get_json()["message"]


def URLError_factory(reason):
    from urllib.error import URLError

    return URLError(reason)


def test_pasted_whitespace_in_a_url_is_reported_clearly(app):
    """A trailing space must not surface as a urllib "control characters" error."""
    from services.health import validate_url

    error = validate_url("https://tulas.schoolv1.com/ dtgfed")

    assert error is not None
    assert "whitespace" in error.lower()
    assert "control character" not in error.lower()


def test_whitespace_url_fails_the_check_without_a_network_call(
    app, make_application
):
    application = make_application(url="https://example.com/ extra")

    result = probe(application)

    assert result["health"] == "DOWN"
    assert "whitespace" in result["message"].lower()


def test_create_application_rejects_a_whitespace_url(admin_client):
    response = admin_client.post(
        "/api/applications",
        headers=JSON_HEADERS,
        json={"name": "spaced", "url": "https://example.com/ oops"},
    )

    assert response.status_code == 422
    assert "whitespace" in str(response.get_json()).lower()
