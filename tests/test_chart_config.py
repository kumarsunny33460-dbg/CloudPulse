"""The Helm chart's ConfigMap/Secret split must match what the app treats as a secret.

Helm is not available in the unit test environment, so the two templates'
decision logic is reproduced here. That is still worth pinning: the failure it
guards against is silent. A value the operator sets in `values.yaml` either
reaches the pod or it does not, and nothing in `helm install` says which.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    not (pathlib.Path(__file__).resolve().parents[1] / "chart" / "cloudpulse").is_dir(),
    reason="chart directory not present",
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHART = ROOT / "chart" / "cloudpulse"

# Must stay in step with the "cloudpulse.secretKeys" template helper.
SECRET_KEYS = {
    "SECRET_KEY",
    "DATABASE_URL",
    "NOTIFY_WEBHOOK_URL",
    "NOTIFY_WEBHOOK_SECRET",
    "NOTIFY_SMTP_USER",
    "NOTIFY_SMTP_PASSWORD",
}

# Must stay in step with the "cloudpulse.structuralKeys" template helper.
STRUCTURAL_KEYS = {
    "createSecret",
    "existingSecret",
    "extra",
    "extraConfigMap",
    "extraSecret",
}


@pytest.fixture(scope="module")
def values() -> dict:
    return yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def config(values) -> dict:
    return values["config"]


def config_map_keys(config: dict) -> set:
    return {k for k in config if k not in SECRET_KEYS and k not in STRUCTURAL_KEYS}


def secret_keys(config: dict) -> set:
    return {k for k in config if k in SECRET_KEYS and k not in STRUCTURAL_KEYS}


# ---------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------

def test_helper_and_test_agree_on_the_secret_list():
    """A divergence here would make every other assertion a lie."""
    helpers = (CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    block = re.search(
        r'define "cloudpulse\.secretKeys".*?list(.*?)-', helpers, re.DOTALL
    )
    assert block, "cloudpulse.secretKeys helper not found"

    declared = set(re.findall(r'"([A-Z0-9_]+)"', block.group(1)))
    assert declared == SECRET_KEYS


def test_helper_and_test_agree_on_the_structural_list():
    helpers = (CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    block = re.search(
        r'define "cloudpulse\.structuralKeys".*?list(.*?)-', helpers, re.DOTALL
    )
    assert block, "cloudpulse.structuralKeys helper not found"

    declared = set(re.findall(r'"([A-Za-z0-9_]+)"', block.group(1)))
    assert declared == STRUCTURAL_KEYS


def test_no_key_is_rendered_twice(config):
    overlap = config_map_keys(config) & secret_keys(config)
    assert not overlap, f"rendered into both objects: {sorted(overlap)}"


def test_structural_keys_are_never_emitted(config):
    for label, produced in (
        ("ConfigMap", config_map_keys(config)),
        ("Secret", secret_keys(config)),
    ):
        leaked = STRUCTURAL_KEYS & produced
        assert not leaked, f"{label} would contain {sorted(leaked)}"


def test_nothing_configured_is_silently_dropped(config):
    """A value in values.yaml that reaches neither object has no effect."""
    rendered = config_map_keys(config) | secret_keys(config)
    dropped = set(config) - rendered - STRUCTURAL_KEYS
    assert not dropped, f"configured but rendered nowhere: {sorted(dropped)}"


def test_credentials_never_reach_the_configmap(config):
    assert not (config_map_keys(config) & SECRET_KEYS)


# ---------------------------------------------------------------------
# Coverage of the settings added for security and retention
# ---------------------------------------------------------------------

SECURITY_AND_RETENTION_KEYS = (
    "LOGIN_MAX_ATTEMPTS",
    "LOGIN_ATTEMPT_WINDOW_SECONDS",
    "LOGIN_LOCKOUT_SECONDS",
    "REGISTRATION_MAX_ATTEMPTS",
    "PASSWORD_RESET_MAX_ATTEMPTS",
    "PASSWORD_RESET_TOKEN_TTL_SECONDS",
    "EMAIL_VERIFICATION_TOKEN_TTL_SECONDS",
    "REQUIRE_EMAIL_VERIFICATION",
    "MIN_PASSWORD_LENGTH",
    "CSP_ENABLED",
    "AUDIT_LOG_RETENTION_DAYS",
    "NOTIFICATION_RETENTION_DAYS",
    "RETENTION_JOB_HOURS",
    "LOG_FORMAT",
    "PUBLIC_BASE_URL",
)


@pytest.mark.parametrize("key", SECURITY_AND_RETENTION_KEYS)
def test_security_and_retention_setting_is_configurable(key, config):
    assert key in config, f"{key} is not configurable through the chart"
    assert key in config_map_keys(config), f"{key} would not reach the pod"


def test_every_named_setting_exists_in_the_application():
    """Guards against a values key that no longer maps to a real setting."""
    source = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s{4}([A-Z][A-Z0-9_]+)\s*=", source, re.MULTILINE))

    for key in SECURITY_AND_RETENTION_KEYS:
        assert key in declared, f"{key} is not a setting in app/config.py"
