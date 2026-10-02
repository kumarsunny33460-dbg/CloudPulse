"""Container configuration must not assume a local checkout layout.

The image mounts the application at its root, so two things that work on a
developer's machine break inside the container: the SQLite fallback directory
resolves to ``/instance``, and the schema is never created. Both were found by
actually building and running the image, not by reading it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[1] / "app" / "Dockerfile"
COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"

dockerfile = DOCKERFILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# The instance directory
# ---------------------------------------------------------------------


def test_instance_dir_is_overridable(monkeypatch, tmp_path):
    """The fallback directory has to be settable, because the default
    calculation assumes ``<repo>/app/config.py`` and the image has
    ``/app/config.py``."""
    import importlib
    from pathlib import Path

    import config as config_module

    target = tmp_path / "somewhere" / "else"
    monkeypatch.setenv("INSTANCE_DIR", str(target))
    importlib.reload(config_module)

    assert config_module.default_instance_dir() == target

    # The URI is rendered with as_posix(), so compare paths rather than strings.
    uri = config_module.resolve_database_uri()
    assert uri.startswith("sqlite:///")
    assert Path(uri[len("sqlite:///") :]).parent == target
    assert target.is_dir(), "the directory should have been created"


def test_instance_dir_defaults_next_to_the_package():
    import config as config_module

    assert config_module.default_instance_dir() == config_module.BASE_DIR.parent / "instance"


def test_dockerfile_sets_an_instance_dir():
    # ENV lines continue with a backslash, so the value is not at end of line.
    assert re.search(r"INSTANCE_DIR=\S+", dockerfile), (
        "the image must set INSTANCE_DIR, because the default resolves to / "
        "inside the container"
    )


def test_dockerfile_instance_dir_is_under_a_writable_path():
    """The image is read-only for the application user above /home."""
    match = re.search(r"INSTANCE_DIR=(\S+)", dockerfile)
    assert match, "INSTANCE_DIR not set"
    assert match.group(1).startswith("/home/"), (
        f"INSTANCE_DIR={match.group(1)} is not writable by the unprivileged user"
    )


# ---------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------


def test_dockerfile_creates_the_schema():
    """Otherwise the container boots, answers /health, and 500s everywhere else."""
    assert re.search(r"AUTO_CREATE_SCHEMA=true", dockerfile), (
        "the image must set AUTO_CREATE_SCHEMA, or the SQLite fallback has no "
        "tables and every endpoint that queries one returns 500"
    )


def test_dockerfile_creates_the_instance_directory():
    assert "mkdir -p /home/cloudpulse/instance" in dockerfile


# ---------------------------------------------------------------------
# The healthcheck must not need a package install
# ---------------------------------------------------------------------


def test_dockerfile_does_not_install_curl():
    """The healthcheck uses urllib from the standard library.

    Installing curl added an apt-get round trip, which fails on any host that
    cannot reach the Debian mirrors, and pulled a package in for one URL.
    """
    assert "apt-get install" not in dockerfile
    assert "urllib" in dockerfile


def test_healthcheck_hits_the_health_endpoint():
    # The command is on the line after HEALTHCHECK, and an explanatory comment
    # sits between them, so the window has to span both.
    assert re.search(r"HEALTHCHECK[\s\S]{0,600}?/health", dockerfile), (
        "the declared healthcheck must call /health"
    )


def test_healthcheck_probes_the_assigned_port():
    """The image reads $PORT, so the probe must too.

    A healthcheck that probes a fixed port marks a correct deploy unhealthy on
    every PaaS that assigns the listen port at run time.
    """
    assert "os.getenv('PORT'" in dockerfile or 'os.getenv("PORT"' in dockerfile


# ---------------------------------------------------------------------
# It must actually run
# ---------------------------------------------------------------------

pytestmark_run = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not available"
)


@pytestmark_run
def test_image_builds():
    root = DOCKERFILE.parents[1]
    result = subprocess.run(
        [
            "docker", "build", "-q", "-t", "cloudpulse:test",
            "-f", str(DOCKERFILE), str(root / "app"),
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert result.returncode == 0, f"build failed:\n{result.stdout[-3000:]}\n{result.stderr[-2000:]}"


@pytestmark_run
def test_container_serves_every_public_endpoint():
    """Builds, boots and answers -- the check that caught both bugs."""
    import socket
    import time
    import urllib.error
    import urllib.request

    name = "cloudpulse-selftest"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    started = subprocess.run(
        [
            "docker", "run", "-d", "--name", name,
            "-p", f"{port}:5000",
            "-e", "SECRET_KEY=" + "s" * 48,  # long enough to pass the production check
            "cloudpulse:test",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert started.returncode == 0, started.stderr

    try:
        base = f"http://127.0.0.1:{port}"
        # Boot can take a moment under a cold start.
        deadline = time.time() + 90
        healthy = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=3) as response:
                    if response.status == 200:
                        healthy = True
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(1)

        assert healthy, _logs(name)

        for path in ("/health", "/health/ready", "/metrics", "/api/cicd", "/login"):
            with urllib.request.urlopen(f"{base}{path}", timeout=10) as response:
                assert response.status == 200, f"{path} -> {response.status}"

        # The process must be running unprivileged.
        identity = subprocess.run(
            ["docker", "exec", name, "id"],
            capture_output=True, text=True, timeout=60,
        )
        assert "uid=0" not in identity.stdout, f"running as root: {identity.stdout}"

        # And Docker's own healthcheck must pass, since the image declares one.
        deadline = time.time() + 90
        status = "starting"
        while time.time() < deadline and status not in {"healthy", "unhealthy"}:
            status = subprocess.run(
                ["docker", "inspect", "--format={{.State.Health.Status}}", name],
                capture_output=True, text=True, timeout=60,
            ).stdout.strip()
            time.sleep(3)
        assert status == "healthy", f"docker healthcheck: {status}\n{_logs(name)}"

    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)


def _logs(name: str) -> str:
    result = subprocess.run(
        ["docker", "logs", name], capture_output=True, text=True, timeout=60
    )
    return (result.stdout or "")[-3000:] + (result.stderr or "")[-1000:]
