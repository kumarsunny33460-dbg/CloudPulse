"""The kustomize overlays must render, and must apply their own values.

Both checks have caught real defects. Rendering failed because a
``configMapGenerator`` cannot merge into a ConfigMap the base declares as static
YAML. And a patch that renders cleanly can still be silently dropped, leaving
production running staging's configuration, so the values themselves are
asserted.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

OVERLAYS = {
    "production": {
        "MONITOR_INTERVAL_SECONDS": "60",
        "replicas": "3",
    },
    "staging": {
        "MONITOR_INTERVAL_SECONDS": "30",
        "HEALTH_CHECK_RETENTION_DAYS": "7",
        "replicas": "1",
    },
}

pytestmark = pytest.mark.skipif(
    shutil.which("kubectl") is None,
    reason="kubectl is not available on this machine",
)


def render(overlay: str) -> str:
    result = subprocess.run(
        ["kubectl", "kustomize", f"k8s/overlays/{overlay}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"{overlay} failed to render:\n{result.stdout}\n{result.stderr}"
    )
    return result.stdout


def config_map_value(rendered: str, key: str) -> str | None:
    for document in rendered.split("\n---"):
        if "kind: ConfigMap" not in document:
            continue
        match = re.search(
            rf'^\s*{re.escape(key)}:\s*"?([^"\n]+)"?\s*$',
            document,
            re.MULTILINE,
        )
        if match:
            return match.group(1).strip()
    return None


def deployment_replicas(rendered: str) -> str | None:
    for document in rendered.split("\n---"):
        if "kind: Deployment" not in document:
            continue
        match = re.search(r"^\s*replicas:\s*(\d+)\s*$", document, re.MULTILINE)
        if match:
            return match.group(1)
    return None


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_overlay_renders(overlay):
    rendered = render(overlay)
    assert "kind: Deployment" in rendered
    assert "kind: Service" in rendered


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_overlay_applies_its_own_values(overlay):
    """A patch can render cleanly and still be dropped."""
    rendered = render(overlay)

    for key, value in OVERLAYS[overlay].items():
        actual = (
            deployment_replicas(rendered)
            if key == "replicas"
            else config_map_value(rendered, key)
        )
        assert actual == value, f"{overlay}: {key} is {actual!r}, expected {value!r}"


@pytest.mark.parametrize("overlay", sorted(OVERLAYS))
def test_overlay_renders_exactly_one_secret(overlay):
    """An overlay that generates a Secret of the same name as the base would
    collide, and the render would carry two objects with one identity."""
    rendered = render(overlay)
    assert rendered.count("kind: Secret") == 1


def test_the_two_overlays_differ():
    """If both rendered identically, the environment split would be a fiction."""
    staging = render("staging")
    production = render("production")

    assert deployment_replicas(staging) != deployment_replicas(production)
    assert config_map_value(staging, "MONITOR_INTERVAL_SECONDS") != config_map_value(
        production, "MONITOR_INTERVAL_SECONDS"
    )
