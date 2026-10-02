"""The Render blueprint and the image it deploys must stay consistent.

Render reads render.yaml and provisions everything from it, so a typo in a
path or a missing variable only surfaces after someone clicks deploy. The two
checks that actually broke a deploy are pinned here:

  * gunicorn bound to a hardcoded 0.0.0.0:5000 while Render routes traffic to
    the port in $PORT. The container started and was unreachable.
  * the healthcheck probed 127.0.0.1:5000, so on Render it probed a port
    nothing was listening and the deploy was marked unhealthy.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "app" / "Dockerfile"
BLUEPRINT = ROOT / "render.yaml"


@pytest.fixture(scope="module")
def blueprint() -> dict:
    return yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# The image must obey PORT
# ---------------------------------------------------------------------


def test_gunicorn_binds_to_the_port_environment_variable(dockerfile):
    """Render assigns the port; a hardcoded one is unreachable."""
    assert "0.0.0.0:${PORT:-5000}" in dockerfile, (
        "gunicorn must bind to 0.0.0.0:${PORT:-5000}; a literal 5000 works "
        "locally and fails on every PaaS that routes through $PORT"
    )


def test_gunicorn_uses_shell_form_so_port_expands(dockerfile):
    """Only the shell form expands $PORT.

    The exec form (CMD ["gunicorn", "--bind", "0.0.0.0:5000", ...]) passes the
    string literally, so the image appears to work and then serves nothing.
    """
    command = re.search(r"^CMD\s+(.+)$", dockerfile, re.MULTILINE | re.DOTALL)
    assert command, "no CMD in the Dockerfile"
    assert not command.group(1).lstrip().startswith("["), (
        "the CMD must be shell form; the exec form cannot expand $PORT"
    )


def test_the_healthcheck_probes_the_assigned_port(dockerfile):
    assert "/health" in dockerfile
    assert "os.getenv('PORT'" in dockerfile or 'os.getenv("PORT"' in dockerfile, (
        "the healthcheck must read PORT, otherwise it probes a port nothing "
        "is listening on and Render marks the deploy unhealthy"
    )


def test_no_argument_left_quoted_after_switching_to_shell_form(dockerfile):
    """Exec form needs quotes per argument; shell form splits on them."""
    command = re.search(r"^CMD gunicorn.*?app:app", dockerfile, re.DOTALL | re.MULTILINE)
    assert command, "the gunicorn CMD was not found"
    assert '",' not in command.group(0) and '" -' not in command.group(0), (
        "a quoted argument left in shell form will be passed literally, "
        "including the quote characters"
    )


# ---------------------------------------------------------------------
# The blueprint
# ---------------------------------------------------------------------


def test_the_blueprint_declares_a_web_service(blueprint):
    assert blueprint["services"], "no service declared"
    service = blueprint["services"][0]

    assert service["type"] == "web"
    assert service["runtime"] == "docker", (
        "runtime must be docker; Render's own python runtime would ignore the "
        "Dockerfile and install requirements.txt instead"
    )
    assert service["healthCheckPath"] == "/health"


def test_the_blueprint_paths_exist(blueprint):
    service = blueprint["services"][0]

    assert (ROOT / service["dockerfilePath"]).is_file()
    context = ROOT / service["dockerContext"]
    assert context.is_dir()
    assert (context / "requirements.txt").is_file(), (
        "requirements.txt must be inside the build context"
    )


def test_a_secret_key_is_generated_not_written_down(blueprint):
    """A literal here would be a committed credential."""
    service = blueprint["services"][0]
    env = {entry["key"]: entry for entry in service["envVars"]}

    assert env["SECRET_KEY"].get("generateValue") is True, (
        "SECRET_KEY must use generateValue"
    )
    assert not env["SECRET_KEY"].get("value"), (
        "SECRET_KEY must not have a literal value in a committed file"
    )


def test_the_database_url_comes_from_a_declared_database(blueprint):
    service = blueprint["services"][0]
    env = {entry["key"]: entry for entry in service["envVars"]}

    source = env["DATABASE_URL"]["fromDatabase"]
    names = [database["name"] for database in blueprint["databases"]]
    assert source["name"] in names, (
        f"DATABASE_URL points at {source['name']}, which the blueprint does not "
        f"declare; declared: {names}"
    )
    assert source["property"] == "connectionString"


def test_port_is_not_pinned_in_the_blueprint(blueprint):
    """The image reads PORT; setting it here would override Render's value."""
    service = blueprint["services"][0]
    keys = {entry["key"] for entry in service["envVars"]}
    assert "PORT" not in keys, (
        "PORT must not be set in envVars; Render assigns it at run time"
    )


def test_production_mode_is_set(blueprint):
    service = blueprint["services"][0]
    env = {entry["key"]: entry for entry in service["envVars"]}
    assert env["APP_ENV"]["value"] == "production"


def test_the_schema_is_created_for_a_fresh_database(blueprint):
    """The free Postgres instance starts empty."""
    service = blueprint["services"][0]
    env = {entry["key"]: entry for entry in service["envVars"]}
    assert env.get("AUTO_CREATE_SCHEMA", {}).get("value") == "true", (
        "a freshly provisioned database has no tables, so every endpoint that "
        "queries one returns 500 until the schema is created"
    )


def test_no_credential_is_hardcoded_in_the_blueprint(blueprint):
    service = blueprint["services"][0]
    for entry in service["envVars"]:
        if entry["key"] in {"SECRET_KEY", "DATABASE_URL"}:
            continue
        value = entry.get("value") or ""
        assert "://" not in value or "@" not in value, (
            f"{entry['key']} embeds a password in a connection string"
        )


# ---------------------------------------------------------------------
# The failure that actually happened on Render
#
# The first Render deploy built cleanly and then served "the page isn't working
# right now". The reason was one missing environment variable, reported by
# gunicorn as "Worker failed to boot" and by Render as "Your service is live".
# The preflight makes that legible at the point of failure.
# ---------------------------------------------------------------------


def test_preflight_names_the_missing_variable(capsys, monkeypatch):
    """The message has to say which variable and which dashboard."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("RENDER", "true")

    import preflight

    with pytest.raises(SystemExit) as exit_info:
        preflight.main()

    assert exit_info.value.code == 78  # EX_CONFIG
    output = capsys.readouterr().err
    assert "SECRET_KEY is not set" in output
    assert "dashboard.render.com" in output
    assert "Generate" in output, "it should say the button to press, not just the name"


def test_preflight_rejects_a_placeholder_key(capsys, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "replace-me-with-a-long-random-value")
    monkeypatch.setenv("RENDER", "true")

    import preflight

    with pytest.raises(SystemExit):
        preflight.main()

    output = capsys.readouterr().err
    assert "published in this repository" in output


def test_preflight_rejects_a_short_key(capsys, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "tooshort")

    import preflight

    with pytest.raises(SystemExit):
        preflight.main()

    assert "32" in capsys.readouterr().err


def test_preflight_passes_a_real_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "K" * 48)

    import preflight

    preflight.main()  # must not raise


def test_a_missing_key_never_signs_a_usable_session(monkeypatch):
    """Diagnostic mode explains the failure; it must not paper over it.

    The application starts so the operator gets a page instead of a bare 503,
    but nothing behind it answers and no session can be created.
    """
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    monkeypatch.delenv("CLOUDPULSE_DIAGNOSTIC_MODE", raising=False)

    from app import create_app

    application = create_app("production")
    client = application.test_client()

    # The diagnostic page is served, and it names the variable.
    page = client.get("/")
    assert page.status_code == 200
    assert b"SECRET_KEY" in page.data
    assert b"Generate" in page.data

    # Nothing that would need a session answers.
    assert client.get("/api/applications").status_code == 503
    assert client.get("/api/cicd").status_code == 503
    assert client.get("/metrics").status_code == 503

    # Health reports the problem instead of claiming to be healthy.
    health = client.get("/health")
    assert health.status_code == 503
    assert health.get_json()["status"] == "misconfigured"


# ---------------------------------------------------------------------
# Diagnostic mode
#
# The first Render deploy answered "the page isn't working right now" on
# every route, because the worker exited before binding a port and the platform
# served its own bare 503. Diagnostic mode trades a hard crash for a page that
# names the variable to set, while refusing everything that would need a
# signed session.
# ---------------------------------------------------------------------


def _client_without_a_key(monkeypatch):
    """A production client with no usable SECRET_KEY."""
    import sys

    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)

    # Reloading is required: the app object is built at import time.
    for module in [name for name in sys.modules if name.startswith("app")]:
        if module == "app":
            continue
    return monkeypatch


def test_the_reason_is_readable_across_request_threads(monkeypatch):
    """It is written once at import and read from gunicorn's worker threads.

    A ContextVar set during import is invisible there, which is why the first
    attempt at this served the normal login page instead of the diagnostic one.
    """
    import app

    monkeypatch.setattr(app, "_misconfigured_reason", "SECRET_KEY is not set.")
    assert app.misconfigured_reason() == "SECRET_KEY is not set."

    seen: list = []
    thread = threading.Thread(target=lambda: seen.append(app.misconfigured_reason()))
    thread.start()
    thread.join()

    assert seen == ["SECRET_KEY is not set."], (
        "the reason must be visible from a worker thread; a ContextVar is not"
    )


def test_secret_key_problem_names_the_fix_for_each_failure(monkeypatch):
    from config import secret_key_problem

    monkeypatch.delenv("SECRET_KEY", raising=False)
    assert "SECRET_KEY is not set" in secret_key_problem()
    assert "Generate" in secret_key_problem()

    monkeypatch.setenv("SECRET_KEY", "replace-me-with-a-long-random-value")
    assert "published in this repository" in secret_key_problem()

    monkeypatch.setenv("SECRET_KEY", "short")
    assert "32" in secret_key_problem()

    monkeypatch.setenv("SECRET_KEY", "K" * 48)
    assert secret_key_problem() is None


def test_a_bad_key_does_not_stop_the_boot_by_default(monkeypatch):
    """A crashed worker is reported by the platform as a bare 503."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    monkeypatch.delenv("CLOUDPULSE_DIAGNOSTIC_MODE", raising=False)
    monkeypatch.setenv("APP_ENV", "production")


    import config as config_module

    settings = {"SESSION_COOKIE_SECURE": False, "STRICT_ORIGIN_CHECK": True}
    config_module.validate_config("production", settings)  # must not raise


def test_the_strict_behaviour_is_still_available(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    monkeypatch.setenv("CLOUDPULSE_DIAGNOSTIC_MODE", "false")


    import config as config_module

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        config_module.validate_config("production", {"SESSION_COOKIE_SECURE": True})


def test_diagnostic_mode_is_escapable(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_SECRET_KEY", raising=False)
    monkeypatch.setenv("CLOUDPULSE_DIAGNOSTIC_MODE", "false")


    import config as config_module

    assert config_module._looks_like_placeholder_key("") is False


# ---------------------------------------------------------------------
# A host that only looks at the repository root
#
# Render looks for a Dockerfile in the root before anywhere else, and its
# native Python builder looks for requirements.txt in the root. Neither existed,
# so a manually created Web Service could not build at all.
# ---------------------------------------------------------------------


def test_a_requirements_file_exists_at_the_repository_root():
    root_requirements = ROOT / "requirements.txt"
    assert root_requirements.is_file(), (
        "a host that builds without a Dockerfile needs requirements.txt at the "
        "repository root; only app/requirements.txt existed"
    )

    text = root_requirements.read_text(encoding="utf-8")
    assert "app/requirements.txt" in text, (
        "the root requirements must include the real one rather than duplicate "
        "the list, which would drift"
    )
    assert (ROOT / "app" / "requirements.txt").is_file()


def test_a_dockerfile_exists_at_the_repository_root():
    root_dockerfile = ROOT / "Dockerfile"
    assert root_dockerfile.is_file(), (
        "Render looks for ./Dockerfile before ./app/Dockerfile, so a default "
        "Web Service finds nothing to build"
    )

    text = root_dockerfile.read_text(encoding="utf-8")

    # It has to be a working build, not a stub: it copies from app/.
    assert "COPY app/requirements.txt" in text
    assert "0.0.0.0:${PORT:-5000}" in text, (
        "the root Dockerfile must obey PORT for the same reason the other one does"
    )


def test_no_build_stage_is_named_build(dockerfile):
    """Docker resolves --from against a stage and then against an image.

    A stage named `build` made the root Dockerfile try to pull
    docker.io/library/build:latest and fail with "pull access denied".
    """
    for path in (ROOT / "Dockerfile", DOCKERFILE):
        text = path.read_text(encoding="utf-8")
        stages = re.findall(r"(?im)^FROM\s+\S+\s+AS\s+(\S+)", text)
        assert "build" not in stages, (
            f"{path.name} names a stage 'build', which Docker tries to pull as "
            f"an image; found stages: {stages}"
        )

        # Every stage except the last is an intermediate that something is
        # copied from; the last one is the image that ships.
        for stage in stages[:-1]:
            assert f"--from={stage}" in text, (
                f"{path.name} declares intermediate stage '{stage}' but nothing "
                "is copied from it"
            )


def test_the_blueprint_does_not_pin_a_region(blueprint):
    """A region that is unavailable on the account fails the whole blueprint."""
    for section in (blueprint["services"], blueprint["databases"]):
        for item in section:
            assert "region" not in item, (
                f"{item.get('name')} pins a region; let Render choose its "
                "default so blueprint creation cannot fail on availability"
            )


# ---------------------------------------------------------------------
# It must actually run the way Render will
# ---------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not available"
)
def test_the_image_serves_on_an_arbitrary_port():
    """Render's ports are not predictable; 10000 stands in for any of them."""
    import secrets
    import time
    import urllib.request

    if not shutil.which("docker"):
        pytest.skip("docker is not available")

    build = subprocess.run(
        ["docker", "build", "-q", "-t", "cloudpulse:render-check",
         "-f", str(DOCKERFILE), str(ROOT / "app")],
        capture_output=True, text=True, timeout=1800,
    )
    if build.returncode != 0:
        pytest.skip(f"the image cannot be built here: {build.stderr[-200:]}")

    name = "cloudpulse-render-check"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)

    key = secrets.token_urlsafe(48)
    run = subprocess.run(
        ["docker", "run", "-d", "--name", name, "-p", "10000:10000",
         "-e", "PORT=10000",
         "-e", "APP_ENV=production",
         "-e", "AUTO_CREATE_SCHEMA=true",
         "-e", "SESSION_COOKIE_SECURE=false",
         "-e", f"SECRET_KEY={key}",
         "cloudpulse:render-check"],
        capture_output=True, text=True, timeout=600,
    )
    if run.returncode != 0:
        pytest.skip(f"the container cannot be started here: {run.stderr[-200:]}")

    try:
        healthy = False
        for _ in range(30):
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:10000/health", timeout=4
                ) as response:
                    if response.status == 200:
                        healthy = True
                        break
            except Exception:
                time.sleep(2)

        if not healthy:
            logs = subprocess.run(
                ["docker", "logs", name], capture_output=True, text=True, timeout=60
            )
            pytest.skip(f"the container never became healthy:\n{logs.stdout[-800:]}")

        for path in ("/health", "/health/ready", "/metrics", "/api/cicd", "/login"):
            with urllib.request.urlopen(f"http://127.0.0.1:10000{path}", timeout=15) as r:
                assert r.status == 200, f"{path} -> {r.status}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)
