"""Content checks across all pages.

Why this file exists: a page can answer with HTTP 200 and still be broken. The endpoints catch
exceptions and show them in an error box -- correct behaviour, but a status-code test sees none of
it. That is exactly how a missing constant name once slipped through: every route 200, and the
variable table simply gone.
"""

from __future__ import annotations

import os

import re

import pytest
from fastapi.testclient import TestClient

from cib7explorer.web.app import app

#: The profile the integration tests run against. Same source as the ``db`` fixture in
#: conftest.py -- a hard-coded name here would make the fixture and the URLs disagree.
PROFILE = os.environ.get("CIB7_TEST_PROFILE", "demo-dump")

#: Strings that have no business appearing on a working page.
BROKEN_MARKERS = ("box-error", "is not defined", "Traceback", "jinja2.exceptions",
          "UndefinedError", "TypeError", "AttributeError", "Internal Server Error")


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _instance_id(client: TestClient) -> str | None:
    from cib7explorer import config
    from cib7explorer.db import connection

    try:
        with connection.connect(config.get_profile(PROFILE)) as db:
            row = db.fetch("""SELECT proc_inst_id_ FROM act_hi_procinst
                              WHERE super_process_instance_id_ IS NULL LIMIT 1""", limit=1)
            return row.rows[0][0] if row.rows else None
    except Exception:  # noqa: BLE001
        return None


@pytest.mark.integration
def test_no_page_shows_an_error(client, busiest_def_key, sample_business_key):
    iid = _instance_id(client)
    if iid is None:
        pytest.skip("no reachable database")
    # The drill-down is called with a pair that may well have no transition: an empty result is a
    # legitimate answer and must not produce an error box either.
    pages = [
        "/", "/health", "/marks",
        f"/profile/{PROFILE}", f"/profile/{PROFILE}/detection",
        f"/profile/{PROFILE}/definitions", f"/profile/{PROFILE}/definitions/{busiest_def_key}",
        f"/profile/{PROFILE}/variables", f"/profile/{PROFILE}/variables/cross-process",
        f"/profile/{PROFILE}/case", f"/profile/{PROFILE}/case?q={sample_business_key[:4]}",
        f"/profile/{PROFILE}/case/{sample_business_key}",
        f"/profile/{PROFILE}/case/{sample_business_key}/correlation",
        f"/profile/{PROFILE}/instance/{iid}",
        f"/profile/{PROFILE}/landscape",
        f"/profile/{PROFILE}/landscape/cases"
        f"?kind=transition&a={busiest_def_key}&b={busiest_def_key}",
    ]
    errors: list[str] = []
    for path in pages:
        r = client.get(path)
        if r.status_code != 200:
            errors.append(f"{path}: HTTP {r.status_code}")
            continue
        text = r.text
        for marker in BROKEN_MARKERS:
            if marker in text:
                at = text.index(marker)
                errors.append(f"{path}: {marker!r} — {text[at:at + 120]!r}")
                break
    assert not errors, "\n".join(errors)


@pytest.mark.integration
def test_every_page_carries_its_core_content(client, busiest_def_key, sample_business_key):
    """A page that is empty but error-free is a failure too.

    The expected content is stated as structure plus one subject taken from the database, never
    as a name from a particular installation.
    """
    iid = _instance_id(client)
    if iid is None:
        pytest.skip("no reachable database")
    expected = {
        "/": ["Connect and inspect", "Cases", "Marks"],
        f"/profile/{PROFILE}/definitions": [busiest_def_key, "validation only", "median"],
        f"/profile/{PROFILE}/variables": ["Variable catalogue", "Present in"],
        f"/profile/{PROFILE}/case/{sample_business_key}": ["call trees", "Gaps",
                                                     "different key", "/instance/"],
        f"/profile/{PROFILE}/instance/{iid}": ["Activities", "value-dialog", "value-button", "Variables"],
        f"/profile/{PROFILE}/landscape": ["Sequence variety", "not a process model", "Co-occurrence"],
        "/marks": ["Marks"],
    }
    missing: list[str] = []
    for path, parts in expected.items():
        text = client.get(path).text
        for part in parts:
            if part not in text:
                missing.append(f"{path}: '{part}' missing")
    assert not missing, "\n".join(missing)


@pytest.mark.integration
def test_explanations_sit_where_the_action_is(client):
    """A button has to say what it does, right next to it."""
    start = client.get("/").text
    assert "Opens a read-only connection" in start, "the connect button needs an explanation"
    assert "nothing is changed" in start

    catalog = client.get(f"/profile/{PROFILE}/definitions").text
    hits = re.search(r"(Rebuild|Build) catalogue", catalog)
    assert hits, "the catalogue button is missing"
    env = catalog[hits.end():hits.end() + 700]
    assert "database" in env and "background" in env, (
        "the catalogue button has to say what it does and how long it takes")


@pytest.mark.integration
def test_the_timeline_links_every_instance(client, sample_business_key):
    """From the timeline it must be possible to jump straight into an instance."""
    text = client.get(f"/profile/{PROFILE}/case/{sample_business_key}").text
    # Only the timeline lanes, not every collapsible on the page (the note list and the
    # instance table are <details> too).
    lanes = re.findall(r'<details class="tl-row">\s*<summary>.*?</summary>', text, re.S)
    assert len(lanes) > 10, f"only {len(lanes)} timeline lanes found"
    without_link = [s for s in lanes if f"/profile/{PROFILE}/instance/" not in s]
    assert not without_link, f"{len(without_link)} of {len(lanes)} lanes do not lead into an instance"
