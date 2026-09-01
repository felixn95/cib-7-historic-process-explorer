"""Tests for the instance detail view: value resolution and the rendered page."""

from __future__ import annotations

import os

import pathlib
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from cib7explorer import config, values
from cib7explorer.contracts import Classification
from cib7explorer.db import instance, sqlguard
from cib7explorer.web.app import app

#: The profile the integration tests run against. Same source as the ``db`` fixture in
#: conftest.py -- a hard-coded name here would make the fixture and the URLs disagree.
PROFILE = os.environ.get("CIB7_TEST_PROFILE", "demo-dump")
EVERYTHING = values.ValueAccess(values.ValuePolicy.ALL)
NOTHING = values.ValueAccess(values.ValuePolicy.NONE, reason="Value mode is off.")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _row(**kw):
    base_path = dict(name_="x", var_type_="string", text_="abc", text2_=None, long_=None,
                 double_=None, bytearray_id_=None, byte_len=None, text_len=3, bytes_text=None)
    base_path.update(kw)
    return base_path


# --- SQL --------------------------------------------------------------------------------

def test_every_sql_constant_passes_the_guard():
    for name, val in vars(instance).items():
        if name.startswith("_SQL_") and isinstance(val, str):
            sqlguard.check(val)


def test_binary_values_are_not_converted_blindly():
    """`serializable` values are Java object streams -- convert_from would fail on them, and an
    object stream is not a readable value in any case."""
    assert "'json', 'xml'" in instance._SQL_VARIABLES
    assert "serializable" not in instance._SQL_VARIABLES.split("CASE WHEN")[1].split("END")[0]


# --- Resolving a value --------------------------------------------------------------------

def test_a_blocked_value_is_not_shown():
    value, allowed, reason, _, _ = instance._render_value(_row(), NOTHING, "order-8000")
    assert value is None and allowed is False
    assert "Value mode" in reason


def test_types_are_resolved_correctly():
    cases = [
        (_row(var_type_="boolean", long_=1, text_=None), "true"),
        (_row(var_type_="boolean", long_=0, text_=None), "false"),
        (_row(var_type_="integer", long_=42, text_=None), "42"),
        (_row(var_type_="double", double_=1.5, text_=None), "1.5"),
        (_row(var_type_="string", text_="hello"), "hello"),
    ]
    for row, expected in cases:
        value, allowed, *_ = instance._render_value(row, EVERYTHING, "d")
        assert allowed and value == expected, row["var_type_"]


def test_an_empty_string_is_a_value():
    value, _, _, _, _ = instance._render_value(_row(text_=""), EVERYTHING, "d")
    assert value == "(empty string)"


def test_type_null_means_present_without_a_value():
    value, allowed, reason, _, _ = instance._render_value(
        _row(var_type_="null", text_=None), EVERYTHING, "d")
    assert value is None and allowed
    assert "carries no value" in reason


def test_a_small_json_value_comes_along_directly():
    value, _, _, _, on_request = instance._render_value(
        _row(var_type_="json", text_=None, bytearray_id_="b1", byte_len=280,
               bytes_text='{"a":1}'), EVERYTHING, "d")
    assert value == '{"a":1}'
    assert on_request is False


def test_a_large_value_only_on_request():
    value, allowed, reason, _, on_request = instance._render_value(
        _row(var_type_="json", text_=None, bytearray_id_="b1",
               byte_len=values.AUTO_LOAD_MAX_BYTES * 4), EVERYTHING, "d")
    assert value is None and allowed and on_request
    assert "on explicit request" in reason


def test_a_binary_value_only_on_request():
    value, _, reason, _, on_request = instance._render_value(
        _row(var_type_="serializable", text_=None, bytearray_id_="b1", byte_len=100,
               text2_="com.example.Foo"), EVERYTHING, "d")
    assert value is None and on_request
    assert "binary" in reason


def test_long_text_is_truncated_for_the_table_and_says_so():
    """The table carries only a preview; the full value arrives through load_value when the dialog
    is opened. That keeps the page small and sends nothing to the browser that nobody looks
    at."""
    value, _, _, truncated, _ = instance._render_value(_row(text_="x" * 5000), EVERYTHING, "d")
    assert truncated and len(value) == instance.PREVIEW_CHARS
    assert instance.PREVIEW_CHARS <= 400


def test_a_blocked_value_stays_blocked_even_when_small():
    """Order of the checks: the value policy first, the size second."""
    value, allowed, _, _, _ = instance._render_value(
        _row(var_type_="json", text_=None, bytearray_id_="b1", byte_len=10,
               bytes_text="{}"), NOTHING, "d")
    assert value is None and allowed is False


# --- The interface -------------------------------------------------------------------------

@pytest.mark.integration
def test_instance_page_against_a_real_database(client, db, sample_instance_id):
    iid = sample_instance_id
    def_key = db.scalar(
        "SELECT proc_def_key_ FROM act_hi_procinst WHERE proc_inst_id_ = %s", (iid,))
    r = client.get(f"/profile/{PROFILE}/instance/{iid}")
    assert r.status_code == 200
    assert def_key in r.text, "the page has to name the definition the instance belongs to"
    assert "svg" in r.text and "is-visited" in r.text, "the BPMN should be drawn and highlighted"
    assert "Activities" in r.text

    b = client.get(f"/profile/{PROFILE}/instance/{iid}/bpmn")
    assert b.status_code == 200
    assert "BPMNShape" in b.text
    assert "attachment" in b.headers.get("content-disposition", "")
    assert "-v" in b.headers["content-disposition"], "the file name carries the instance version"


@pytest.mark.integration
def test_an_unknown_instance_says_so_politely(client):
    r = client.get(f"/profile/{PROFILE}/instance/does-not-exist")
    assert r.status_code == 200
    assert "no entry for instance id" in r.text or "history cleanup" in r.text


@pytest.mark.integration
def test_the_start_trigger_is_identical_in_both_views(db, profile, sample_business_key):
    """One instance must not get two different answers in the case view and the detail view."""
    from cib7explorer.db import case

    v = case.load_case(db, profile, sample_business_key)
    for node in list(v.instances)[:8]:
        det = instance.load_instance(db, profile, node.proc_inst_id)
        assert det is not None
        assert det.instance.start_trigger is node.start_trigger, node.def_key


@pytest.mark.integration
def test_value_on_request(client, db, profile):
    row = db.fetch("""SELECT v.proc_inst_id_, v.name_, v.bytearray_id_, v.proc_def_key_
                      FROM act_hi_varinst v
                      WHERE v.bytearray_id_ IS NOT NULL AND v.var_type_ = 'json'
                      LIMIT 1""", limit=1)
    pid, name, ba, dk = row.rows[0]
    r = client.get(f"/profile/{PROFILE}/instance/{pid}/value",
                   params={"variable": name, "bytearray": ba, "def_key": dk})
    assert r.status_code == 200
    assert "value" in r.text or "Java object stream" in r.text


# --- the value dialog on click (from a report: "I see no values") --------------------------

@pytest.mark.integration
def test_the_instance_page_shows_no_error_box(client, db):
    """The page answers with 200 even when a template is broken -- that is exactly how a missing
    constant name once slipped past a status-code test. So this test checks the content."""
    row = db.fetch("SELECT proc_inst_id_ FROM act_hi_procinst LIMIT 1", limit=1)
    html = client.get(f"/profile/{PROFILE}/instance/{row.rows[0][0]}").text
    assert "box-error" not in html, "the page is showing an error box"
    assert "is not defined" not in html and "Traceback" not in html


@pytest.mark.integration
def test_the_variable_table_is_clickable_and_compact(client, variable_rich_instance_id):
    html = client.get(f"/profile/{PROFILE}/instance/{variable_rich_instance_id}").text
    assert "value-dialog" in html, "the value dialog has to be present on the page"
    assert html.count("value-button") > 20, "the values have to be clickable"
    # No multi-kilobyte blob in the table any more. A cell carries the preview (at most 200
    # characters, twice for copyable values: visible and inside the copy button's data-text), plus
    # a link and the copy icon -- which is why the bound is not the preview length itself.
    import re

    cells = [len(z) for z in re.findall(r'<td class="value-cell">(.*?)</td>', html, re.S)]
    assert cells
    assert max(cells) < 1600, f"value cell too large ({max(cells)} B) — the preview should be short"
    from statistics import median

    assert median(cells) < 1100, f"median value cell too large ({median(cells)} B)"


@pytest.mark.integration
def test_the_value_json_endpoint_returns_the_full_value(client, db):
    # Deliberately a variable longer than the preview -- there are also
    # one-byte JSON values, on which this test would demonstrate nothing.
    row = db.fetch("""SELECT v.proc_inst_id_, v.name_
                        FROM act_hi_varinst v
                        JOIN act_ge_bytearray b ON b.id_ = v.bytearray_id_
                       WHERE v.var_type_ = 'json'
                         AND octet_length(b.bytes_) BETWEEN 1000 AND 4000
                       LIMIT 1""", limit=1)
    pid, name = row.rows[0]
    d = client.get(f"/profile/{PROFILE}/instance/{pid}/value.json", params={"variable": name}).json()
    assert d["allowed"] is True
    assert d["formattable"] is True, "JSON has to be indentable"
    assert d["value"] and len(d["value"]) > instance.PREVIEW_CHARS, (
        "the dialog has to deliver more than the preview in the table")


@pytest.mark.integration
def test_the_value_json_endpoint_respects_the_value_policy(client, db, monkeypatch):
    from cib7explorer import config as cfg
    from cib7explorer.contracts import Classification
    from cib7explorer.web import deps as web_deps

    row = db.fetch("SELECT proc_inst_id_, name_ FROM act_hi_varinst LIMIT 1", limit=1)
    pid, name = row.rows[0]
    prof = cfg.get_profile(PROFILE)
    locked = cfg.with_overrides(prof, classification=Classification.PROD, values_mode=None)
    monkeypatch.setattr(web_deps, "get_profile_or_404", lambda n: locked)
    d = client.get(f"/profile/{PROFILE}/instance/{pid}/value.json", params={"variable": name}).json()
    assert d["allowed"] is False
    assert not d.get("value")


@pytest.mark.integration
def test_a_binary_value_at_least_yields_the_raw_bytes(client, db):
    row = db.fetch("""SELECT proc_inst_id_, name_ FROM act_hi_varinst
                      WHERE var_type_ = 'serializable' AND bytearray_id_ IS NOT NULL LIMIT 1""",
                   limit=1)
    pid, name = row.rows[0]
    d = client.get(f"/profile/{PROFILE}/instance/{pid}/value.json", params={"variable": name}).json()
    assert d["allowed"] is True
    assert d.get("raw_bytes"), "for Java object streams the raw bytes should be visible"
    assert "not resolvable as text" in (d.get("hint") or "")


# --- from a report: "I see no values, there is no column at all" ---------------------------

@pytest.mark.integration
def test_the_value_column_sits_up_front_not_off_to_the_right(client, variable_rich_instance_id):
    """The column used to be the sixth of six and hid behind the table's horizontal scrollbar in
    a narrow window -- all that was visible were name, type, size, scope and timestamp. The value
    now sits directly next to the name."""
    import re

    html = client.get(f"/profile/{PROFILE}/instance/{variable_rich_instance_id}").text
    table_html = re.findall(r'<table[^>]*var-table[^>]*>(.*?)</table>', html, re.S)[0]
    header = [re.sub(r"<[^>]+>", "", t).strip()
              for t in re.findall(r"<th[^>]*>(.*?)</th>", table_html, re.S)]
    assert len(header) <= 4, f"too many columns, the value slides off to the right: {header}"
    assert "Value" in header[1], f"the value has to be the second column, was: {header}"


@pytest.mark.integration
def test_variables_come_before_the_activity_list(client, db):
    """Variables belong directly after the model and before the activity list."""
    import re

    row = db.fetch("SELECT proc_inst_id_ FROM act_hi_procinst LIMIT 1", limit=1)
    html = client.get(f"/profile/{PROFILE}/instance/{row.rows[0][0]}").text
    headings = [re.sub(r"<[^>]+>", "", m).strip()
                for m in re.findall(r"<h2>(.*?)</h2>", html, re.S)]
    core = [u.split(" (")[0] for u in headings]
    assert core.index("Variables") < core.index("Activities"), core
    if "Model" in core:
        assert core.index("Model") < core.index("Variables"), core


@pytest.mark.integration
def test_values_are_reachable_without_javascript(client, db):
    """The value does not depend on the dialog: without JavaScript the link leads to a page."""
    row = db.fetch("""SELECT v.proc_inst_id_, v.name_, v.proc_def_key_
                        FROM act_hi_varinst v
                       WHERE v.var_type_ = 'string' AND v.text_ IS NOT NULL
                         AND length(v.text_) > 3 LIMIT 1""", limit=1)
    pid, name, def_key = row.rows[0]
    r = client.get(f"/profile/{PROFILE}/instance/{pid}/value",
                   params={"variable": name, "def_key": def_key})
    assert r.status_code == 200
    assert "back to the instance" in r.text, "the page needs a way back"
    assert "value-block" in r.text, "the value has to be rendered as a block"


@pytest.mark.integration
def test_static_files_carry_a_version_stamp(client):
    """Otherwise the browser keeps stale CSS and a fixed interface looks unchanged -- with
    everything correct on the server side, which makes it miserable to diagnose."""
    html = client.get("/").text
    assert "style.css?v=" in html
    assert "value.js?v=" in html or "value.js" not in html


@pytest.mark.integration
def test_a_copy_icon_next_to_every_value(client, variable_rich_instance_id):
    """A small copy icon next to every value.

    Two routes, because the table carries only a preview: complete values sit in the button
    itself (``data-text``), truncated and binary ones are fetched when copying (``data-url``) --
    what gets copied is always the whole value, never the truncated preview.
    """
    import html as html_mod
    import json
    import re

    page = client.get(f"/profile/{PROFILE}/instance/{variable_rich_instance_id}").text

    buttons = re.findall(r'<button[^>]*class="copy-button"[^>]*>', page)
    values = re.findall(r'<a class="value-button"', page)
    assert len(buttons) == len(values), (
        f"{len(buttons)} copy icons for {len(values)} values — there should be one next to each")
    assert len(buttons) > 20

    direct = [k for k in buttons if "data-text=" in k]
    deferred = [k for k in buttons if "data-url=" in k]
    assert direct and deferred, "both copy routes should occur"
    assert len(direct) + len(deferred) == len(buttons)

    # A directly copyable JSON value has to survive attribute escaping intact.
    for hit in re.finditer(r'data-text="([^"]*)"', page):
        raw = html_mod.unescape(hit.group(1))
        if raw.startswith("{"):
            json.loads(raw)          # raises if escaping destroyed the value
            break


def test_copying_never_copies_the_preview():
    """A guarantee about the code: the copy route for truncated values fetches them instead of
    taking the truncated preview."""
    js = (pathlib.Path(instance.__file__).parent.parent / "web" / "static" / "value.js").read_text(
        encoding="utf-8")
    assert "data-url" in js and "data-text" in js
    assert "raw_bytes" in js, "binary values must be copyable too"
    assert "execCommand" in js, "fallback for browsers without the clipboard API"
