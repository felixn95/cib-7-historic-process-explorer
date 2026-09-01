"""The path prefix behind a proxy (`CIB7_BASE_PATH`).

Why this file exists: the bug it prevents is cheap to make and expensive to find. A single
new `href="/profile/..."` in a template never shows up while the tool runs at `/` -- and behind
a proxy it leads to the proxy's 404, which looks like a defect of this tool and is not one.

Hence two levels: the static test across all templates catches the next new place, and the render
test proves that the prefix really ends up in the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEMPLATES = Path(__file__).parent.parent / "cib7explorer" / "web" / "templates"

#: Attributes whose value is a path, and which therefore have to carry the prefix.
PATH_ATTRIBUTES = ("href", "src", "action", "hx-get", "hx-post")

_ATTR_ABSOLUTE = re.compile(r'\b(' + "|".join(PATH_ATTRIBUTES) + r')="/')
#: Paths that Jinja assembles itself (`{% set url = "/profile/" ~ ... %}`).
_JINJA_ABSOLUTE = re.compile(r'=\s*"/(profile|marks|static)')


def _all_templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def test_there_are_templates_to_check_at_all():
    # Otherwise the test below would stay quietly green if the directory ever moved.
    assert len(_all_templates()) >= 15


def test_no_template_builds_an_absolute_path_without_the_prefix():
    findings: list[str] = []
    for path in _all_templates():
        for lineno, row in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _ATTR_ABSOLUTE.search(row) or _JINJA_ABSOLUTE.search(row):
                findings.append(f"{path.name}:{lineno}: {row.strip()}")
    assert not findings, (
        "Absolute path without `{{ base_path }}` -- behind a proxy with a prefix such a link "
        "points nowhere:\n" + "\n".join(findings)
    )


def test_the_prefix_is_normalised(monkeypatch):
    from cib7explorer import config

    for configured, expected in (
        (None, ""),
        ("", ""),
        ("/", ""),
        ("/process-explorer", "/process-explorer"),
        ("process-explorer", "/process-explorer"),
        ("/process-explorer/", "/process-explorer"),
        ("  /process-explorer/  ", "/process-explorer"),
    ):
        if configured is None:
            monkeypatch.delenv("CIB7_BASE_PATH", raising=False)
        else:
            monkeypatch.setenv("CIB7_BASE_PATH", configured)
        assert config.base_path() == expected, configured


def test_urls_built_in_python_carry_the_prefix(monkeypatch):
    """`case_url` and `instance_url` are the two links that are not built in a template."""
    from cib7explorer.web.views_cases import case_url
    from cib7explorer.web.views_instance import instance_url

    monkeypatch.delenv("CIB7_BASE_PATH", raising=False)
    assert case_url("p", "BK-1").startswith("/profile/p/case/")
    assert instance_url("p", "42").startswith("/profile/p/instance/")

    monkeypatch.setenv("CIB7_BASE_PATH", "/process-explorer")
    assert case_url("p", "BK-1") == "/process-explorer/profile/p/case/BK-1"
    # Business keys with a slash use the query variant -- with the prefix too.
    assert case_url("p", "JetDock/416").startswith("/process-explorer/profile/p/case?key=")
    assert instance_url("p", "42") == "/process-explorer/profile/p/instance/42"


@pytest.fixture()
def client_with_prefix(monkeypatch, tmp_path):
    """The interface with a prefix set, without a database and without any real state.

    The profiles file points at a host that does not exist: the start page lists profiles without
    connecting, and that is exactly what should be rendered here.
    """
    profiles_file = tmp_path / "profiles.yaml"
    profiles_file.write_text(
        "profiles:\n"
        "  - name: test-profile\n"
        "    kind: direct\n"
        "    classification: test\n"
        "    host: nowhere.invalid\n"
        "    database: camunda\n"
        "    user: explorer_ro\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIB7_BASE_PATH", "/process-explorer")
    monkeypatch.setenv("CIB7_PROFILES", str(profiles_file))
    monkeypatch.setenv("CIB7_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CIB7_NOTES", str(tmp_path / "marks.sqlite"))

    from cib7explorer.web.app import app

    with TestClient(app) as client:
        yield client


def test_the_start_page_carries_the_prefix_in_every_link(client_with_prefix):
    response = client_with_prefix.get("/")
    assert response.status_code == 200
    text = response.text

    # The prefix really arrived ...
    assert 'href="/process-explorer/profile/test-profile"' in text
    assert 'href="/process-explorer/static/style.css' in text

    # ... and no link was left without it.
    without_prefix = [
        hits.group(0)
        for hits in re.finditer(r'\b(?:' + "|".join(PATH_ATTRIBUTES) + r')="/[^"]*', text)
        if not hits.group(0).split('="', 1)[1].startswith("/process-explorer")
    ]
    assert not without_prefix, "links without the prefix in the rendered page: " + repr(without_prefix)


def test_without_a_prefix_nothing_changes(monkeypatch, tmp_path):
    """Serving under `/` must not change because the prefix capability exists."""
    profiles_file = tmp_path / "profiles.yaml"
    profiles_file.write_text(
        "profiles:\n"
        "  - name: test-profile\n"
        "    kind: direct\n"
        "    classification: test\n"
        "    host: nowhere.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CIB7_BASE_PATH", raising=False)
    monkeypatch.setenv("CIB7_PROFILES", str(profiles_file))
    monkeypatch.setenv("CIB7_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CIB7_NOTES", str(tmp_path / "marks.sqlite"))

    from cib7explorer.web.app import app

    with TestClient(app) as client:
        text = client.get("/").text
    assert 'href="/profile/test-profile"' in text
    assert "/process-explorer" not in text
