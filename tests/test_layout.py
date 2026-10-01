"""Dashboard shell and modal layout contracts.

These are static regression guards. The failures they cover are invisible in
unit tests but obvious in a browser: a modal whose footer scrolls out of reach,
a duplicated element id, or a JavaScript module querying an element that the
template no longer contains.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

APP_DIR = Path(__file__).resolve().parents[1] / "app"
STATIC_JS = APP_DIR / "static" / "js"
TEMPLATE = APP_DIR / "templates" / "index.html"
STYLESHEET = APP_DIR / "static" / "style.css"

JS_MODULES = (
    "core.js",
    "charts.js",
    "overview.js",
    "applications.js",
    "incidents.js",
    "deployments.js",
    "activity.js",
    "main.js",
)


def _html(client) -> str:
    with client.session_transaction() as session:
        session["user_id"] = 1
        session["username"] = "layout-test"
        session["role"] = "Admin"
    return client.get("/").get_data(as_text=True)


@pytest.fixture
def dashboard(client):
    return _html(client)


@pytest.fixture
def css():
    return STYLESHEET.read_text(encoding="utf-8")


@pytest.fixture
def js():
    return "\n".join((STATIC_JS / name).read_text(encoding="utf-8") for name in JS_MODULES)


# ---------------------------------------------------------------------
# Modal layout
# ---------------------------------------------------------------------

def test_modal_is_a_bounded_flex_column(css):
    assert re.search(r"\.modal \{[^}]*display:\s*flex", css, re.S)
    assert re.search(r"\.modal \{[^}]*flex-direction:\s*column", css, re.S)
    assert re.search(r"\.modal \{[^}]*max-height:", css, re.S)
    assert re.search(r"\.modal \{[^}]*overflow:\s*hidden", css, re.S)


def test_modal_body_can_actually_scroll(css):
    """A flex child needs min-height:0 or it refuses to shrink and scrolls nowhere."""
    assert re.search(r"\.modal-body \{[^}]*overflow-y:\s*auto", css, re.S)
    assert re.search(r"\.modal-body \{[^}]*min-height:\s*0", css, re.S)
    assert re.search(r"\.modal-body \{[^}]*flex:\s*1 1 auto", css, re.S)


def test_form_between_modal_and_body_keeps_the_flex_chain(css):
    assert re.search(r"\.modal > form \{[^}]*display:\s*flex", css, re.S)
    assert re.search(r"\.modal > form \{[^}]*flex-direction:\s*column", css, re.S)
    assert re.search(r"\.modal > form \{[^}]*min-height:\s*0", css, re.S)


def test_header_and_footer_never_shrink(css):
    assert re.search(r"\.modal-header \{[^}]*flex:\s*0 0 auto", css, re.S)
    assert re.search(r"\.modal-footer \{[^}]*flex:\s*0 0 auto", css, re.S)


# ---------------------------------------------------------------------
# Modal content
# ---------------------------------------------------------------------

def _modal_blocks(html: str) -> dict:
    return {
        match.group(1): match.group(2)
        for match in re.finditer(
            r'<div class="modal-backdrop" id="(\w+)"(.*?)\n</div>\n', html, re.S
        )
    }


def test_every_modal_has_a_header_and_a_body(dashboard):
    blocks = _modal_blocks(dashboard)
    assert set(blocks) == {
        "appFormModal",
        "healthHistoryModal",
        "incidentFormModal",
        "incidentDetailModal",
        "deploymentFormModal",
    }

    for name, body in blocks.items():
        assert "modal-header" in body, name
        assert "modal-body" in body, name


def test_every_form_modal_has_a_footer_and_submit(dashboard):
    for name in ("appFormModal", "incidentFormModal", "deploymentFormModal"):
        body = _modal_blocks(dashboard)[name]
        assert "modal-footer" in body, f"{name} has no footer"
        assert 'type="submit"' in body, f"{name} has no submit button"


def test_every_form_modal_submit_is_inside_a_form(dashboard):
    for name in ("appFormModal", "incidentFormModal", "deploymentFormModal"):
        body = _modal_blocks(dashboard)[name]
        assert re.search(r"<form[^>]*>.*modal-body.*type=\"submit\".*</form>", body, re.S), name


def test_application_form_collapses_advanced_options(dashboard):
    body = _modal_blocks(dashboard)["appFormModal"]
    assert 'id="appAdvancedFields"' in body
    assert "optional-fields" in body

    fields_inside = body.split("optional-fields-body", 1)[1]
    for field_id in ("appMethod", "appTimeout", "appInterval", "appExpected"):
        assert field_id in fields_inside, field_id

    visible_part = body.split("optional-fields", 1)[0]
    for field_id in ("appName", "appUrl", "appEnvironment", "appStatus"):
        assert field_id in visible_part, field_id


# ---------------------------------------------------------------------
# Template wiring
# ---------------------------------------------------------------------

def test_no_duplicate_element_ids(dashboard):
    ids = re.findall(r'id="([A-Za-z0-9_-]+)"', dashboard)
    duplicates = {value for value in ids if ids.count(value) > 1}
    assert not duplicates, f"duplicate ids: {sorted(duplicates)}"


def test_every_element_the_script_queries_exists(dashboard, js):
    html_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', dashboard))

    referenced = set()
    referenced |= set(re.findall(r"[\$]\(\s*[\"']#([A-Za-z0-9_-]+)[\"']\s*\)", js))
    referenced |= set(re.findall(r"getElementById\(\s*[\"']([A-Za-z0-9_-]+)[\"']\s*\)", js))
    referenced |= set(re.findall(r"[\$][\$]\(\s*[\"']#([A-Za-z0-9_-]+)", js))
    referenced |= set(re.findall(r"querySelector(?:All)?\(\s*[\"']#([A-Za-z0-9_-]+)", js))
    referenced |= set(re.findall(r"modal\.(?:open|close)\(\s*[\"']([A-Za-z0-9_-]+)[\"']", js))
    referenced |= set(re.findall(r"[\"']#([A-Za-z][A-Za-z0-9_-]*)[\"']\s*,\s*\d", js))

    missing = sorted(value for value in referenced if value not in html_ids)
    assert not missing, f"script queries missing elements: {missing}"


def test_all_scripts_are_loaded(dashboard):
    loaded = set(re.findall(r'src="[^"]*/js/([^"]+)"', dashboard))
    assert loaded == set(JS_MODULES), f"loaded {sorted(loaded)}"


def test_stylesheet_is_linked_once(dashboard):
    assert len(re.findall(r'href="[^"]*style\.css"', dashboard)) == 1


def test_view_panels_have_unique_ids(dashboard):
    views = re.findall(r'<section class="view[^"]*" id="view-(\w+)"', dashboard)
    assert len(views) == len(set(views))
    assert set(views) == {"overview", "applications", "incidents", "deployments", "activity"}


# ---------------------------------------------------------------------
# Static asset integrity
# ---------------------------------------------------------------------

_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^<>")


def _strip_javascript(text: str) -> str:
    """Remove strings, comments and regex literals, keeping the code structure.

    A plain regular expression is not enough: a ``//`` inside a string such as
    ``"http://www.w3.org/2000/svg"`` looks like a comment, and a parenthesised
    group inside a regex literal such as ``/^(https?):/`` looks like unbalanced
    code. This walks the source once and tracks which state it is in.
    """
    out: list[str] = []
    state = "code"
    quote = ""
    previous = ""

    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        following = text[index + 1] if index + 1 < length else ""

        if state == "line-comment":
            if char == "\n":
                state = "code"
                out.append(char)
        elif state == "block-comment":
            if char == "*" and following == "/":
                state = "code"
                index += 1
        elif state in {"string", "template", "regex"}:
            if char == "\\":
                out.append(" ")
                index += 1
                if index < length:
                    out.append(text[index])
            elif (state == "regex" and char == "/") or (
                state != "regex" and char == quote
            ):
                state = "code"
        else:
            if char == "/" and following == "/":
                state = "line-comment"
                index += 1
            elif char == "/" and following == "*":
                state = "block-comment"
                index += 1
            elif char == "/" and (previous in _REGEX_PRECEDERS or previous == ""):
                state = "regex"
                index += 1
            elif char in "\"'`":
                state = "template" if char == "`" else "string"
                quote = char
            else:
                out.append(char)

        if not char.isspace():
            previous = char
        index += 1

    assert state not in {"string", "template", "regex", "block-comment"}, "unterminated literal"
    return "".join(out)


@pytest.mark.parametrize("name", JS_MODULES)
def test_javascript_modules_have_balanced_delimiters(name):
    stripped = _strip_javascript((STATIC_JS / name).read_text(encoding="utf-8"))

    assert stripped.count("{") == stripped.count("}"), name
    assert stripped.count("(") == stripped.count(")"), name
    assert stripped.count("[") == stripped.count("]"), name


def test_stylesheet_braces_are_balanced(css):
    without_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert without_comments.count("{") == without_comments.count("}")


def test_dashboard_does_not_reference_a_remote_script(dashboard):
    """Charts are rendered locally, so the dashboard must not need a CDN."""
    remote = re.findall(r'src="(https?://[^"]+)"', dashboard)
    assert not remote, f"remote scripts: {remote}"
