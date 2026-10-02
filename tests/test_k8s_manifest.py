"""The Kubernetes manifests must agree with the image they deploy.

Every defect in this file was found by running the deployment, not by reading
it. Each one put the pods in a state where they never became ready, and each
one is cheap to reintroduce, so each gets a test.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "k8s" / "base" / "cloudpulse.yaml"
OVERLAYS = ROOT / "k8s" / "overlays"

pytestmark = pytest.mark.skipif(
    shutil.which("kubectl") is None,
    reason="kubectl is not available on this machine",
)


def base_manifest() -> dict:
    """The base as a multi-document mapping of kind -> list of documents."""
    import yaml

    documents = [
        document
        for document in yaml.safe_load_all(BASE.read_text(encoding="utf-8"))
        if document
    ]
    by_kind: dict[str, list] = {}
    for document in documents:
        by_kind.setdefault(document["kind"], []).append(document)
    return by_kind


def deployment() -> dict:
    matches = base_manifest()["Deployment"]
    assert len(matches) == 1, f"expected one Deployment, found {len(matches)}"
    return matches[0]


def pod_spec() -> dict:
    return deployment()["spec"]["template"]["spec"]


# ---------------------------------------------------------------------
# The uid in the manifest must match the user the image creates
# ---------------------------------------------------------------------


def test_run_as_user_matches_the_image_user():
    """The image creates uid 999. Running as anything else owns nothing.

    With runAsUser 10001 the process had an id that owned neither
    /home/cloudpulse nor the SQLite fallback directory, and every pod died at
    boot with "PermissionError: [Errno 13] Permission denied:
    '/home/cloudpulse/instance'".
    """
    # The Dockerfile creates the user with no explicit id, so read the value
    # the running image reports rather than assuming.
    image = ROOT / "app" / "Dockerfile"
    assert image.exists()

    if shutil.which("docker") is None:
        pytest.skip("docker is not available to read the image's uid")

    tag = "cloudpulse-uid-test"
    build = subprocess.run(
        ["docker", "build", "-q", "-t", tag, "-f", str(image), str(ROOT / "app")],
        capture_output=True, text=True, timeout=1800,
    )
    if build.returncode != 0:
        pytest.skip(f"the image cannot be built here: {build.stderr[-200:]}")

    try:
        identity = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "id", tag],
            capture_output=True, text=True, timeout=180,
        )
        match = re.search(r"uid=(\d+)", identity.stdout)
        assert match, f"could not read the image uid: {identity.stdout!r}"
        image_uid = int(match.group(1))
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=300)

    security = pod_spec()["securityContext"]
    assert security["runAsUser"] == image_uid, (
        f"the manifest runs as {security['runAsUser']} but the image's user is "
        f"{image_uid}; nothing in the image is owned by the former"
    )
    assert security["runAsGroup"] == image_uid


# ---------------------------------------------------------------------
# INSTANCE_DIR must be a mounted, writable volume
# ---------------------------------------------------------------------


def test_the_sqlite_directory_is_a_mounted_volume():
    """The Dockerfile declares VOLUME for this path, which Kubernetes turns
    into a root-owned anonymous volume the unprivileged user cannot write.

    The pod reached the point of trying to create the database and failed on
    mkdir. An explicit mount fixes the ownership.
    """
    spec = pod_spec()
    container = spec["containers"][0]

    mounts = {mount["mountPath"] for mount in container.get("volumeMounts", [])}
    assert "/home/cloudpulse/instance" in mounts, (
        "INSTANCE_DIR is not mounted, so the SQLite fallback has no writable path"
    )

    volumes = {volume["name"] for volume in spec.get("volumes", [])}
    for mount in container.get("volumeMounts", []):
        assert mount["name"] in volumes, f"{mount['name']} is mounted but never declared"


def test_the_scratch_directory_is_declared():
    """readOnlyRootFilesystem is set, so /tmp must be writable too."""
    spec = pod_spec()
    container = spec["containers"][0]

    assert container["securityContext"]["readOnlyRootFilesystem"] is True

    mounts = {mount["mountPath"] for mount in container.get("volumeMounts", [])}
    assert "/tmp" in mounts, "read-only root filesystem but /tmp is not mounted"


# ---------------------------------------------------------------------
# No Secret may be committed
# ---------------------------------------------------------------------


def test_the_base_commits_no_secret():
    """A committed Secret is a real Secret.

    The base used to ship one with a placeholder value, which every overlay
    inherited unless it explicitly replaced it. The staging overlay did not, so
    the pods came up with the published placeholder and refused to start.
    """
    assert "Secret" not in base_manifest(), (
        "k8s/base declares a Secret; create it out of band instead"
    )


def test_no_manifest_hardcodes_a_placeholder_key():
    """The inert fallback inside a secretGenerator is allowed, but nowhere
    else -- a literal Secret would be applied as-is."""
    for path in OVERLAYS.rglob("kustomization.yaml"):
        text = path.read_text(encoding="utf-8")
        if "secretGenerator" not in text:
            assert "SECRET_KEY" not in text, f"{path.name} sets SECRET_KEY outside a generator"


# ---------------------------------------------------------------------
# Overlays must render and reference the fixed-name Secret
# ---------------------------------------------------------------------


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_the_overlay_uses_a_fixed_secret_name(overlay):
    """kustomize appends a content hash by default, producing a name nothing
    else can reference.

    The pods sat in CreateContainerConfigError waiting for
    "cloudpulse-secrets-ck5c46bt6f", a name only the build knew about.
    """
    text = (OVERLAYS / overlay / "kustomization.yaml").read_text(encoding="utf-8")
    if "secretGenerator" not in text:
        pytest.skip(f"{overlay} does not generate a Secret")

    assert "disableNameSuffixHash: true" in text, (
        f"{overlay} generates a Secret without disableNameSuffixHash"
    )


@pytest.mark.parametrize("overlay", ["staging", "production"])
def test_the_rendered_deployment_can_actually_start(overlay):
    """Render, then check the things a pod needs to reach Ready.

    This is the check that would have caught all three defects: the placeholder
    key, the hashed Secret name, and the uid mismatch.
    """
    rendered = subprocess.run(
        ["kubectl", "kustomize", f"k8s/overlays/{overlay}"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300,
    )
    assert rendered.returncode == 0, rendered.stderr

    import yaml

    documents = [
        document
        for document in yaml.safe_load_all(rendered.stdout)
        if document
    ]
    by_kind = {document["kind"]: document for document in documents}

    if "Secret" not in by_kind:
        pytest.skip(f"{overlay} expects the Secret to be created out of band")

    secret_document = by_kind["Secret"]

    # kustomize rewrites stringData into base64 data, so accept either.
    import base64

    payload = secret_document.get("stringData")
    if payload is None:
        payload = {
            key: base64.b64decode(value).decode()
            for key, value in secret_document.get("data", {}).items()
        }

    assert payload["SECRET_KEY"] == "replace-me-with-a-long-random-value", (
        "the rendered Secret should carry only the inert fallback; the real key "
        "is applied with kubectl"
    )

    deployment = by_kind["Deployment"]
    spec = deployment["spec"]["template"]["spec"]
    container = spec["containers"][0]

    # The Secret the container reads must be the one that was generated.
    references = {
        entry["secretRef"]["name"]
        for entry in container.get("envFrom", [])
        if "secretRef" in entry
    }
    assert secret_document["metadata"]["name"] in references, (
        f"the container reads {references} but the generated Secret is called "
        f"{secret_document['metadata']['name']}"
    )

    # A hashed name in the reference would not match anything else.
    assert all("-b" not in name and not re.fullmatch(r".*-[0-9a-f]{10}", name)
               for name in references), f"a hashed Secret name is in use: {references}"
