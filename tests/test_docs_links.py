"""Documentation links must work on github.com, not only on the author's machine.

A `localhost` URL rendered as a clickable link resolves on the reader's own
computer, where nothing is listening. Six of them shipped in README.md and were
dead for anyone who opened the repository. Inside a fenced code block the same
URL is correct -- it is an instruction to run something -- so those are exempt.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "docs" / "API.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "docs" / "RUNBOOK.md",
    ROOT / "chart" / "cloudpulse" / "README.md",
]

# <http://localhost:5000>, [text](http://localhost:5000)
CLICKABLE_LOCAL = re.compile(
    r"<https?://(?:localhost|127\.0\.0\.1)[^>]*>|\]\(https?://(?:localhost|127\.0\.0\.1)[^)]*\)"
)


def fenced_line_numbers(text: str) -> set[int]:
    """Line numbers inside a fenced code block, 1-based."""
    inside = False
    numbers: set[int] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("```"):
            inside = not inside
            numbers.add(number)
            continue
        if inside:
            numbers.add(number)
    return numbers


@pytest.mark.parametrize(
    "document", DOCUMENTS, ids=lambda path: path.name
)
def test_no_clickable_localhost_links(document):
    if not document.is_file():
        pytest.skip(f"{document} is not present")

    text = document.read_text(encoding="utf-8")
    fenced = fenced_line_numbers(text)

    offenders = [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), start=1)
        if number not in fenced and CLICKABLE_LOCAL.search(line)
    ]

    assert not offenders, (
        f"{document.name} has clickable localhost links, which are dead on "
        f"github.com:\n"
        + "\n".join(f"  line {n}: {line}" for n, line in offenders)
    )


@pytest.mark.parametrize(
    "document", DOCUMENTS, ids=lambda path: path.name
)
def test_relative_file_links_resolve(document):
    """A relative link works on GitHub only if the file is committed."""
    if not document.is_file():
        pytest.skip(f"{document} is not present")

    text = document.read_text(encoding="utf-8")
    base = document.parent

    missing = []
    for match in re.finditer(r"\[[^\]]*\]\(([^)\s]+)\)", text):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        if not (base / path).exists():
            missing.append(target)

    assert not missing, (
        f"{document.name} links to files that are not in the repository: "
        f"{sorted(set(missing))}"
    )


@pytest.mark.parametrize(
    "document", DOCUMENTS, ids=lambda path: path.name
)
def test_internal_anchors_resolve(document):
    if not document.is_file():
        pytest.skip(f"{document} is not present")

    text = document.read_text(encoding="utf-8")

    headings = {
        re.sub(r"[^a-z0-9 -]", "", line.lstrip("# ").lower()).replace(" ", "-")
        for line in text.splitlines()
        if line.startswith("#")
    }

    unresolved = [
        match.group(1)
        for match in re.finditer(r"\[[^\]]*\]\((#[^)]+)\)", text)
        if match.group(1).lstrip("#") not in headings
    ]

    assert not unresolved, (
        f"{document.name} links to headings that do not exist: {unresolved}"
    )


def test_the_readme_points_at_the_hosted_demo():
    """There should be one link that does work from a browser."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Deploy to Render" in text, (
        "the README should tell a reader how to get a URL that is reachable "
        "from outside their own machine"
    )
    assert "render.yaml" in text
