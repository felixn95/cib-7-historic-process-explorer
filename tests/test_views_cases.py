"""Tests for the case view.

Focus: the geometry of the timeline (computed in Python, therefore checkable), the single time
zone -- a real bug lived there once, with the axis running an hour beside every other timestamp
on the page -- and the guarantee that the second track is never merged into the case.
"""

from __future__ import annotations

import dataclasses

import os

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from cib7explorer import config
from cib7explorer.contracts import (
    Correlation,
    ForeignKeyLink,
    Gap,
    GapKind,
    InstanceNode,
    InstanceOrigin,
    StartTrigger,
    TreeSummary,
    Case,
    CaseNote,
)
from cib7explorer.web import views_cases
from cib7explorer.web.app import app

#: The profile the integration tests run against. Same source as the ``db`` fixture in
#: conftest.py -- a hard-coded name here would make the fixture and the URLs disagree.
PROFILE = os.environ.get("CIB7_TEST_PROFILE", "demo-dump")
W0 = datetime(2026, 8, 3, 8, 0, 0)      # database time (UTC in the test profile)
W1 = datetime(2026, 8, 4, 8, 0, 0)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def profile():
    return config.get_profile(PROFILE)


def _node(minute: int, *, origin=InstanceOrigin.OWN_KEY, key="K1", root="r1", parent=None,
          running=False, depth=0) -> InstanceNode:
    return InstanceNode(
        proc_inst_id=f"i{minute}", def_key="order-1000", def_id="d1", parent_id=parent,
        root_id=root, business_key=key, origin=origin,
        start_time=W0 + timedelta(minutes=minute),
        end_time=None if running else W0 + timedelta(minutes=minute + 1),
        duration_ms=60_000, state=None if running else "COMPLETED",
        start_trigger=StartTrigger.PLAIN_START, depth=depth)


def _case(**kw) -> Case:
    nodes = (_node(0), _node(60, origin=InstanceOrigin.NO_KEY, key=None),
              _node(120, origin=InstanceOrigin.OTHER_KEY, key="P9"))
    base_path = dict(
        key="K1", instances=nodes,
        trees=(TreeSummary(root_id="r1", def_key="order-1000", start_time=W0,
                           end_time=W0 + timedelta(minutes=121), instance_count=3, max_depth=1),),
        gaps=(Gap(kind=GapKind.BETWEEN, start=W0 + timedelta(minutes=1),
                  end=W0 + timedelta(minutes=60), duration_ms=59 * 60_000,
                  after_def="order-1000", before_def="order-3000"),),
        foreign_keys=(ForeignKeyLink(key="P9", instances_in_case=1, instances_total=22,
                                     first_seen=W0),),
        notes=(CaseNote("info", "1 instance carries a different business key (P9)."),),
        window_start=W0, window_end=W1, instances_shown=3, instances_total=3, trees_total=1,
        loaded_at=datetime.now(timezone.utc), duration_ms=42)
    base_path.update(kw)
    return Case(**base_path)


@pytest.fixture
def case_loaded(monkeypatch):
    v = _case()

    class FakeMod:
        BROWSE_ORDERS = {"instances": "most process instances"}
        DENSITY_THRESHOLD = 50

        @staticmethod
        def load_case(db, profile, key, **kw):
            return v

        @staticmethod
        def correlate(db, profile, case, **kw):
            return [Correlation(variable="ticketNumber", value_shown="K1",
                                instances_in_case=3, instances_total=39,
                                outside_ids=("x1", "x2"), outside_def_keys=("ticket-1000",))]

        @staticmethod
        def search_keys(db, term, **kw):
            return []

        @staticmethod
        def browse_keys(db, **kw):
            return []

    monkeypatch.setattr(views_cases, "_load_module", lambda: FakeMod)

    class FakeDB:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(views_cases.deps, "open_database", lambda profile: FakeDB())
    return v


# --- Geometry --------------------------------------------------------------------------

def test_bar_position_and_width():
    bar = views_cases.build_bar(W0 + timedelta(hours=6), W0 + timedelta(hours=12), W0, W1,
                            running=False)
    assert bar.left_pct == pytest.approx(25.0)
    assert bar.width_pct == pytest.approx(25.0)
    assert bar.open_right is False


def test_a_running_bar_reaches_the_edge_and_is_marked_open():
    bar = views_cases.build_bar(W0 + timedelta(hours=12), None, W0, W1, running=True)
    assert bar.left_pct == pytest.approx(50.0)
    assert bar.width_pct == pytest.approx(50.0)
    assert bar.open_right is True


def test_a_short_bar_stays_visible():
    """A median tree duration well under a second inside a window spanning a day. Without a
    minimum width the bar would be invisible and the information lost."""
    bar = views_cases.build_bar(W0, W0 + timedelta(seconds=1), W0, W1, running=False)
    assert bar.width_pct == pytest.approx(views_cases.MIN_BAR_PCT)


def test_a_bar_never_runs_past_the_edge():
    bar = views_cases.build_bar(W1 - timedelta(seconds=1), W1 + timedelta(days=5), W0, W1,
                            running=False)
    assert bar.left_pct + bar.width_pct <= 100.0001


def test_timeline_and_gap_bands():
    v = _case()
    rows, bands, window = views_cases.build_timeline(v)
    assert len(rows) == 1
    assert window == (W0, W1)
    assert len(bands) == 1
    assert bands[0].left_pct == pytest.approx(100 * 1 / (24 * 60), abs=0.01)


# --- Time zone (regression) --------------------------------------------------------------

def test_axis_labels_are_in_the_display_zone(profile):
    """A bug that really happened: the axis formatted the raw database time and therefore ran an
    hour beside every other timestamp on the page."""
    ticks = views_cases.build_ticks(W0, W1, profile, count=2)
    assert ticks[0].label.endswith("10:00"), (
        f"08:00 UTC has to appear as 10:00 Europe/Berlin, was {ticks[0].label!r}")


def test_zoom_input_is_read_as_display_time(profile):
    back = views_cases.from_display_tz(datetime(2026, 8, 3, 10, 0), profile)
    assert back == datetime(2026, 8, 3, 8, 0), "Berlin 10:00 is 08:00 in the database"


def test_timezone_round_trip(profile):
    from cib7explorer.web import deps

    db_time = datetime(2026, 8, 3, 8, 0)
    displayed = deps.to_display_tz(db_time, profile).replace(tzinfo=None)
    assert views_cases.from_display_tz(displayed, profile) == db_time


# --- URLs -------------------------------------------------------------------------------

def test_a_key_without_a_slash_becomes_a_path():
    assert views_cases.case_url("p", "BK-2026-000134") == "/profile/p/case/BK-2026-000134"


def test_a_key_with_a_slash_falls_back_to_the_query():
    """Business keys can contain a slash, and the ASGI server decodes
    %2F before routing, so the path parameter never matches."""
    url = views_cases.case_url("p", "JetDock/416")
    assert url == "/profile/p/case?key=JetDock%2F416"


# --- Pages -----------------------------------------------------------------------------

def test_the_case_start_page(client, case_loaded):
    r = client.get(f"/profile/{PROFILE}/case")
    assert r.status_code == 200
    assert "Browse" in r.text


def test_the_case_page_separates_the_origins(client, case_loaded):
    html = client.get(f"/profile/{PROFILE}/case/K1").text
    assert "carry the key" in html
    assert "without a key of their own" in html
    assert "different key" in html


def test_the_case_page_shows_notes_and_gaps(client, case_loaded):
    html = client.get(f"/profile/{PROFILE}/case/K1").text
    assert "different business key" in html
    assert "Gaps" in html
    assert "59 min" in html or "1 h" in html


def test_a_foreign_key_shows_how_much_lies_outside(client, case_loaded):
    html = client.get(f"/profile/{PROFILE}/case/K1").text
    assert "Outside" in html
    assert "21" in html, "22 in total minus 1 in the case = 21 outside"


def test_the_second_track_stays_separate(client, case_loaded, monkeypatch):
    # The variables have to be configured for this track to run at all -- the tool ships no guess
    # about which variable identifies a business object.
    configured = dataclasses.replace(
        config.get_profile(PROFILE), correlation_variables=("ticketNumber",))
    monkeypatch.setattr(views_cases.deps, "get_profile_or_404", lambda name: configured)
    html = client.get(f"/profile/{PROFILE}/case/K1/correlation").text
    assert "not" in html and "added to the case" in html
    assert "ticketNumber" in html
    assert "36" in html, "39 in total minus 3 in the case = 36 outside"


def test_an_unconfigured_second_track_says_so_instead_of_showing_nothing(client, case_loaded):
    """Without configured variables the track cannot look for anything -- and an empty box would
    read as "nothing found", which is a different statement."""
    html = client.get(f"/profile/{PROFILE}/case/K1/correlation").text
    assert "No correlation variables are configured" in html
    assert "correlation_variables" in html, "the page has to name the setting"


def test_the_second_track_stays_closed_without_value_mode(client, case_loaded, monkeypatch):
    prod = config.Profile(name=PROFILE, classification=config.Classification.PROD)
    monkeypatch.setattr(views_cases.deps, "get_profile_or_404", lambda name: prod)
    html = client.get(f"/profile/{PROFILE}/case/K1/correlation").text
    assert "no allowlist" in html or "hidden" in html
    assert "ticketNumber" not in html


def test_every_instance_leads_into_the_detail_view(client, case_loaded):
    """The jump from the timeline into an instance has to work -- overview into detail and
    back is the whole point of having both."""
    html = client.get(f"/profile/{PROFILE}/case/K1").text
    assert f"/profile/{PROFILE}/instance/i0" in html


def test_unknown_profile(client):
    assert client.get("/profile/no-such-profile/case").status_code == 404


@pytest.mark.integration
def test_a_real_case_page(client, sample_business_key):
    r = client.get(f"/profile/{PROFILE}/case/{sample_business_key}")
    assert r.status_code == 200
    assert "call trees" in r.text
    assert sample_business_key in r.text


@pytest.mark.parametrize("path", ["/case", "/case/K1", "/case/K1/correlation"])
def test_the_table_grid_is_consistent(client, case_loaded, path):
    """The same guarantee as for the definition pages: `.grid` is a display:grid card layout, and
    a table carrying that class loses its own grid."""
    import re

    html = client.get(f"/profile/{PROFILE}{path}").text
    assert 'class="grid"' not in html
    for tbl in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        header = re.findall(r"<thead>(.*?)</thead>", tbl, re.S)
        if not header:
            continue
        ths = re.findall(r"<th([^>]*)>", header[0])
        body = re.findall(r"<tbody>(.*?)</tbody>", tbl, re.S)
        for row in (re.findall(r"<tr>(.*?)</tr>", body[0], re.S) if body else []):
            if "colspan" in row:
                continue
            tds = re.findall(r"<td([^>]*)>", row)
            assert len(tds) == len(ths), f"{path}: {len(ths)} headers, {len(tds)} cells"
            assert [i for i, a in enumerate(ths) if "num" in a] == \
                   [i for i, a in enumerate(tds) if "num" in a]


# --- time window with a calendar ------------------------------------------------------------

def test_calendar_fields_instead_of_text_fields(client, case_loaded):
    """A date picker without a library: `datetime-local` brings the browser's own calendar, and
    min/max clamp it to the period of the case."""
    import re

    html = client.get(f"/profile/{PROFILE}/case/K1").text
    fields = re.findall(r'<input[^>]*type="datetime-local"[^>]*>', html, re.S)
    assert len(fields) == 2, f"from and to should be calendar fields, found: {len(fields)}"
    for field in fields:
        assert 'min="' in field and 'max="' in field, "the calendar should be bounded"
        assert 'step="60"' in field


def test_the_calendar_field_is_prefilled_in_the_right_format(client, case_loaded):
    """A space instead of the T leaves the field empty -- the calendar then opens on nothing."""
    import re

    html = client.get(f"/profile/{PROFILE}/case/K1").text
    values = re.findall(r'<input[^>]*type="datetime-local"[^>]*value="([^"]*)"', html, re.S)
    assert values and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", w) for w in values), values


def test_calendar_bounds_are_the_period_of_the_case(client, case_loaded, profile):
    from cib7explorer.web import deps

    html = client.get(f"/profile/{PROFILE}/case/K1").text
    expected_min = deps.to_display_tz(W0, profile).strftime("%Y-%m-%dT%H:%M")
    expected_max = deps.to_display_tz(W1, profile).strftime("%Y-%m-%dT%H:%M")
    assert f'min="{expected_min}"' in html
    assert f'max="{expected_max}"' in html


@pytest.mark.parametrize("user_input", [
    "2026-08-03T11:00", "2026-08-03T11:00:00", "2026-08-03 11:00", "2026-08-03",
])
def test_zoom_input_understands_every_spelling(user_input, profile):
    """The browser sends a T and, depending on the version, seconds too; links from the density
    band and hand-typed values have to work as well."""
    v = _case()
    window = views_cases._parse_window(user_input, "", v, profile)
    assert window is not None, f"{user_input!r} was not understood"


def test_jump_windows_are_offered(client, case_loaded):
    html = client.get(f"/profile/{PROFILE}/case/K1").text
    assert "first hour" in html, "a case spanning more than two hours should offer this"
    assert "whole case" in html


def test_jump_windows_only_when_they_fit(profile):
    """For a case spanning ten minutes, "last 30 days" is nonsense."""
    short = _case(window_start=W0, window_end=W0 + timedelta(minutes=10))
    assert views_cases._zoom_suggestions(short, profile) == []
    long = _case(window_start=W0, window_end=W0 + timedelta(days=90))
    labels = [b for b, _v, _b in views_cases._zoom_suggestions(long, profile)]
    assert "last 30 days" in labels
