"""Tests for the definition and variable pages.

The focus is on the honesty guarantees: "not recorded" must never appear as 0, every share
carries its denominator, runtimes are distributions rather than averages, and these pages never
show a variable value.
"""

from __future__ import annotations

import os

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cib7explorer.contracts import (
    CatalogMeta,
    CrossProcessVariable,
    DefinitionSummary,
    DurationStats,
    EndActivity,
    SizeStats,
    VariableCatalog,
    VariableCatalogEntry,
)
from cib7explorer.web.app import app

#: The profile the integration tests run against. Same source as the ``db`` fixture in
#: conftest.py -- a hard-coded name here would make the fixture and the URLs disagree.
PROFILE = os.environ.get("CIB7_TEST_PROFILE", "demo-dump")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _definition(**kw) -> DefinitionSummary:
    base = dict(
        key="order-8000", name="order-8000 (renewal)", instances=26700,
        instances_as_root=26700, instances_as_child=0, completed=26600,
        externally_terminated=3, active=118, versions_used=160,
        first_start=datetime(2025, 2, 25, tzinfo=timezone.utc),
        last_start=datetime(2026, 8, 17, tzinfo=timezone.utc),
        distinct_business_keys=1389,
        duration=DurationStats(n=26624, n_unfinished=118, p25=200, p50=397, p75=1200,
                               p90=6100, p99=42000, minimum=90, maximum=900000),
        open_incidents=106, historic_incidents=None,
        user_task_instances=0, distinct_assignees=0,
        end_activities=(EndActivity("EndEvent_ValidationOnly", 4500, True, True),
                        EndActivity("Event_1qwl7cn", 20000, False, False)),
    )
    base.update(kw)
    return DefinitionSummary(**base)


def _variable(**kw) -> VariableCatalogEntry:
    base = dict(def_key="order-8000", name="orderNumber", types=("string",),
                occurrences=26700, instances_with=26500, def_instances=26700,
                inline_size=SizeStats(n=26700, minimum=8, p50=12, p90=14, maximum=20))
    base.update(kw)
    return VariableCatalogEntry(**base)


def _catalog(entries=None, cross=None) -> VariableCatalog:
    return VariableCatalog(
        entries=tuple(entries or [_variable()]),
        cross_process=tuple(cross or []),
        meta=CatalogMeta(built_at=datetime.now(timezone.utc), duration_ms=86000,
                         profile_name=PROFILE, history_level="AUDIT", rows=1,
                         notes=("act_hi_detail is empty at history level AUDIT.",)),
    )


class _FakeRead:
    """Stands in for a cache hit, without SQLite and without a database."""

    def __init__(self, data):
        self.data = data
        self.built_at = datetime.now(timezone.utc)
        self.age_seconds = 12.0
        self.meta = data.meta if isinstance(data, VariableCatalog) else CatalogMeta(
            built_at=self.built_at, rows=len(data) if isinstance(data, list) else 0,
            notes=("act_hi_detail is empty at history level AUDIT.",))


@pytest.fixture
def cache_filled(monkeypatch):
    """Lay out definitions and catalogue as though they had been precomputed."""
    from cib7explorer.web import views_definitions

    state = {"defs": [_definition()], "catalog": _catalog()}

    monkeypatch.setattr(views_definitions, "_get_cache", lambda profile: (object(), None))
    monkeypatch.setattr(views_definitions, "_read_definitions", lambda cache: _FakeRead(state["defs"]))
    monkeypatch.setattr(views_definitions, "_read_catalog", lambda cache: _FakeRead(state["catalog"]))
    return state


@pytest.fixture
def cache_leer(monkeypatch):
    from cib7explorer.web import views_definitions

    monkeypatch.setattr(views_definitions, "_get_cache", lambda profile: (object(), None))
    monkeypatch.setattr(views_definitions, "_read_definitions", lambda cache: None)
    monkeypatch.setattr(views_definitions, "_read_catalog", lambda cache: None)


# --- Reachability ---------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/definitions", "/definitions/order-8000", "/variables",
    "/variables/cross-process", "/catalog/status",
])
def test_routes_respond(client, cache_filled, path):
    r = client.get(f"/profile/{PROFILE}{path}")
    assert r.status_code == 200, r.text[:300]


def test_an_unknown_profile_yields_404_with_a_readable_message(client):
    r = client.get("/profile/no-such-profile/definitions")
    assert r.status_code == 404
    assert "is not known" in r.text


# --- Honesty ------------------------------------------------------------------------

def test_unrecorded_incidents_do_not_appear_as_zero(client, cache_filled):
    html = client.get(f"/profile/{PROFILE}/definitions").text
    assert "not recorded" in html
    # the open count is there; the history is explicitly not shown as 0
    assert "106" in html
    assert "History: 0" not in html


def test_a_real_zero_for_user_tasks_is_shown_as_zero(client, cache_filled):
    """user_task_instances=0 is a real zero and must NOT read "not recorded"."""
    html = client.get(f"/profile/{PROFILE}/definitions/order-8000").text
    assert "User tasks" in html
    row = html[html.index("User tasks"):html.index("User tasks") + 400]
    assert "not recorded" not in row


def test_every_share_carries_its_denominator(client, cache_filled):
    html = client.get(f"/profile/{PROFILE}/definitions").text
    assert "26,700" in html, "a share must always carry its denominator"


def test_no_averages_for_runtimes(client, cache_filled):
    html = client.get(f"/profile/{PROFILE}/definitions/order-8000").text
    for word in ("avg", "arithmetic mean"):
        assert word not in html, f"{word!r} must not appear for runtimes"
    # The word "average" may only appear in an explicit negation ("No average"), never as the
    # label of a number.
    at = 0
    while True:
        at = html.find("average", at)
        if at < 0:
            break
        assert "No average" in html[max(0, at - 4):at + 10], (
            "average appears as a label rather than as a negation: "
            f"{html[max(0, at - 60):at + 40]!r}")
        at += 1
    assert "median" in html
    assert "p90" in html, "the distribution has to be visible, not just a single number"


def test_a_type_switch_is_visible(client, cache_filled):
    cache_filled["catalog"] = _catalog(
        entries=[_variable(types=("boolean", "string"), type_switch=True)])
    html = client.get(f"/profile/{PROFILE}/variables").text
    assert "type switch" in html


def test_a_variable_without_a_denominator_does_not_crash(client, cache_filled):
    cache_filled["catalog"] = _catalog(
        entries=[_variable(def_instances=0, instances_with=0, occurrences=0)])
    r = client.get(f"/profile/{PROFILE}/variables")
    assert r.status_code == 200


def test_the_variables_page_states_that_it_shows_no_values(client, cache_filled):
    html = client.get(f"/profile/{PROFILE}/variables").text
    assert "no variable values" in html


def test_cross_process_is_labelled_as_a_candidate_list(client, cache_filled):
    cache_filled["catalog"] = _catalog(cross=[
        CrossProcessVariable("orderNumber", 60, ("a", "b"), ("string",), False, 80000, 70000)])
    html = client.get(f"/profile/{PROFILE}/variables/cross-process").text
    assert "candidates" in html.lower()
    assert "orderNumber" in html


def test_an_empty_cache_offers_a_build_button_not_an_empty_table(client, cache_leer):
    html = client.get(f"/profile/{PROFILE}/definitions").text
    assert "Build catalogue" in html
    assert "<tbody>" not in html


def test_the_catalogue_age_is_visible(client, cache_filled):
    html = client.get(f"/profile/{PROFILE}/variables").text
    assert "Catalogue built" in html
    assert "rebuild catalogue" in html.lower()


# --- Export -----------------------------------------------------------------------------

def test_csv_export_of_the_variables(client, cache_filled):
    r = client.get(f"/profile/{PROFILE}/variables.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    text = r.content.decode("utf-8")
    assert text.startswith("﻿")
    header = text.lstrip("﻿").splitlines()[0]
    assert ";" in header
    forbidden = {"value", "values", "content", "text_", "bytes_"}
    assert not (forbidden & {c.strip().lower() for c in header.split(";")})


def test_csv_export_of_the_definitions(client, cache_filled):
    r = client.get(f"/profile/{PROFILE}/definitions.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.content.decode("utf-8")
    # The export carries the definitions that the filled cache contains.
    assert body.count("\n") > 1, "a header alone is not an export"
    assert "key" in body.splitlines()[0].lower()


def test_no_password_in_any_response(client, cache_filled):
    from cib7explorer import config

    profile = config.get_profile(PROFILE)
    pw = profile.resolve_password()
    if not pw:
        pytest.skip("the test profile has no resolvable password")
    for path in ("/definitions", "/variables", "/variables/cross-process",
                 "/definitions.csv", "/variables.csv"):
        text = client.get(f"/profile/{PROFILE}{path}").content.decode("utf-8", "replace")
        assert pw not in text, f"the password appears in {path}"


# --- Integration ------------------------------------------------------------------------

@pytest.mark.integration
def test_pages_against_a_real_catalogue(client, db, busiest_def_key, catalog):
    """Without monkeypatching: whatever is in the real cache has to appear on the page."""
    r = client.get(f"/profile/{PROFILE}/definitions")
    assert r.status_code == 200
    if "Catalogue built" not in r.text:
        pytest.skip("no catalogue precomputed for this profile yet")
    assert busiest_def_key in r.text
    v = client.get(f"/profile/{PROFILE}/variables")
    assert v.status_code == 200
    if catalog.entries:
        assert catalog.entries[0].name in v.text


# --- The table grid ---------------------------------------------------------------------

def _tables(html: str):
    import re

    for tbl in re.findall(r'<table[^>]*>(.*?)</table>', html, re.S):
        header = re.findall(r"<thead>(.*?)</thead>", tbl, re.S)
        if not header:
            continue                       # key/value table without a header
        ths = re.findall(r"<th([^>]*)>", header[0])
        body = re.findall(r"<tbody>(.*?)</tbody>", tbl, re.S)
        rows = re.findall(r"<tr>(.*?)</tr>", body[0], re.S) if body else []
        yield ths, rows


@pytest.mark.parametrize("path", ["/definitions", "/definitions/order-8000", "/variables",
                                  "/variables/cross-process"])
def test_tables_have_as_many_cells_as_column_headers(client, cache_filled, path):
    import re

    html = client.get(f"/profile/{PROFILE}{path}").text
    tables = list(_tables(html))
    assert tables, f"{path}: no table with a header row found"
    for ths, rows in tables:
        for row in rows:
            if 'colspan' in row:
                continue                   # empty-state message spanning the full width
            tds = re.findall(r"<td([^>]*)>", row)
            assert len(tds) == len(ths), (
                f"{path}: {len(ths)} column headers but {len(tds)} cells -- "
                "the table is misaligned")


@pytest.mark.parametrize("path", ["/definitions", "/variables"])
def test_numeric_columns_are_right_aligned_in_the_header_too(client, cache_filled, path):
    """Otherwise the column looks misaligned even though the grid is correct."""
    import re

    html = client.get(f"/profile/{PROFILE}{path}").text
    for ths, rows in _tables(html):
        num_header = [i for i, a in enumerate(ths) if "num" in a]
        for row in rows:
            if 'colspan' in row:
                continue
            tds = re.findall(r"<td([^>]*)>", row)
            num_cell = [i for i, a in enumerate(tds) if "num" in a]
            assert num_header == num_cell, (
                f"{path}: right-aligned cells {num_cell}, but headers {num_header}")


def test_no_table_uses_the_card_layout_class(client, cache_filled):
    """In the stylesheet `.grid` is a display:grid card layout. A table carrying that class loses
    its grid entirely -- which was exactly the bug where all
    tables look misaligned."""
    for path in ("/definitions", "/definitions/order-8000", "/variables",
                 "/variables/cross-process"):
        html = client.get(f"/profile/{PROFILE}{path}").text
        assert 'class="grid"' not in html, f"{path}: table carrying class=grid"
        assert "datatable" in html
