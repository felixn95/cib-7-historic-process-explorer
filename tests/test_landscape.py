"""Tests for the landscape: counting without judgement, and the drill-down to the cases."""

from __future__ import annotations

import os

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cib7explorer.contracts import Distribution, Landscape
from cib7explorer.db import landscape as L, sqlguard
from cib7explorer.web import views_landscape
from cib7explorer.web.app import app

#: The profile the integration tests run against. Same source as the ``db`` fixture in
#: conftest.py -- a hard-coded name here would make the fixture and the URLs disagree.
PROFILE = os.environ.get("CIB7_TEST_PROFILE", "demo-dump")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# --- SQL and ground rules -------------------------------------------------------------------

def test_every_sql_constant_passes_the_guard():
    n = 0
    for name, val in vars(L).items():
        if name.startswith("_SQL_") and isinstance(val, str):
            sqlguard.check(val)
            n += 1
    assert n >= 8


def test_no_average_anywhere_in_the_module():
    """No averaged indicator that hides the distribution it came from."""
    source = open(L.__file__, encoding="utf-8").read()
    for forbidden in ("avg(", "AVG(", "mean(", "statistics.mean"):
        assert forbidden not in source, f"{forbidden} hides a distribution"


def test_gap_bucket_keys_are_ascii():
    """They travel as URL parameters through the drill-down, where a non-ASCII character
    produces a 400."""
    for key, _label, _limit in L.GAP_BUCKETS:
        assert key.isascii() and " " not in key


def test_every_gap_bucket_gets_its_own_label_and_range():
    """Each bucket key has to resolve to *its* label and *its* range.

    This is a regression test: a loop variable once shadowed the parameter of the same name, so
    the comparison was always true and every bucket resolved to the first one. The page still
    rendered -- with six identical labels and a drill-down that filtered the wrong range. Nothing
    failed, the numbers were simply wrong, which is the kind of defect this whole tool exists to
    avoid producing.
    """
    labels = [views_landscape.gap_label(key) for key, _l, _limit in L.GAP_BUCKETS]
    assert len(set(labels)) == len(L.GAP_BUCKETS), labels
    for key, label, limit in L.GAP_BUCKETS:
        assert views_landscape.gap_label(key) == label
        lower, upper, resolved = views_landscape._gap_range(key)
        assert (upper, resolved) == (limit, label)
        assert lower < upper


def test_a_distribution_without_values_is_empty_and_does_not_crash():
    d = Distribution.from_values([])
    assert d.n == 0 and d.p50 is None


def test_distribution_quartiles():
    d = Distribution.from_values([float(i) for i in range(1, 101)])
    assert d.p50 == pytest.approx(50, abs=1)
    assert d.p25 == pytest.approx(25, abs=1)
    assert d.p90 == pytest.approx(90, abs=1)


# --- The call graph ------------------------------------------------------------------------

def test_graph_edge_width_follows_frequency():
    from cib7explorer.contracts import CallEdge

    land = Landscape(call_edges=(
        CallEdge("a", "b", 1000, 500), CallEdge("a", "c", 10, 10), CallEdge("d", "e", 1, 1)))
    g = views_landscape._build_graph(land, node_limit=10)
    widths = {(e.parent, e.child): e.width for e in g.edges}
    assert widths[("a", "b")] > widths[("a", "c")] > widths[("d", "e")]
    assert g.total_edges == 3 and g.shown_edges == 3


def test_the_graph_bounds_its_nodes_and_says_what_is_missing():
    from cib7explorer.contracts import CallEdge

    kanten = tuple(CallEdge(f"p{i}", f"c{i}", 100 - i, 10) for i in range(20))
    g = views_landscape._build_graph(Landscape(call_edges=kanten), node_limit=6)
    assert len(g.nodes) == 6
    assert g.shown_edges < g.total_edges
    assert g.shown_calls < g.total_calls, "the omitted calls have to stay visible"


def test_sequence_concentration():
    from cib7explorer.contracts import SequencePattern

    land = Landscape(sequences=tuple(
        [SequencePattern(("a",), 100)] + [SequencePattern((f"x{i}",), 1) for i in range(90)]))
    share = land.sequence_concentration
    assert share is not None and 0.5 < share < 0.62


def test_sequence_concentration_without_data():
    assert Landscape().sequence_concentration is None


# --- Integration ------------------------------------------------------------------------

@pytest.mark.integration
def test_landscape_against_a_real_database(db, landscape):
    """Checked against the database rather than against remembered numbers -- so this test says
    something on any installation, and says it about the computation instead of about one
    dataset."""
    land = landscape
    m = land.meta
    # One pass over the history for all four totals: each of them alone is a sequential scan
    # over millions of rows, and four scans cost four times as much for the same answer.
    # The case level is built from root instances, so the key count is restricted to those.
    counts = db.fetch("""
        SELECT count(*)                                                        AS instances,
               count(DISTINCT proc_def_key_)                                    AS definitions,
               count(DISTINCT business_key_) FILTER (
                   WHERE super_process_instance_id_ IS NULL)                    AS root_keys,
               count(*) FILTER (
                   WHERE super_process_instance_id_ IS NULL
                     AND business_key_ IS NOT NULL)                             AS root_instances
          FROM act_hi_procinst
    """, limit=1).one
    assert m.instances_total == counts["instances"]
    assert m.definitions_total == counts["definitions"]
    assert m.keys_total == counts["root_keys"]
    assert m.root_instances == counts["root_instances"]

    # Roles partition the definitions: every one is a root only, a child only, or both.
    roles = len(land.both_roles) + len(land.only_child) + len(land.only_root)
    assert roles == m.definitions_total, (
        f"{roles} definitions across the three role groups, but {m.definitions_total} in total")
    assert not (set(land.only_child) & set(land.only_root))

    # Call depth: the distribution covers every instance, and depth 0 is the root level.
    depths = dict(land.depth_distribution)
    assert sum(depths.values()) == m.instances_total
    assert depths.get(0, 0) == db.scalar(
        "SELECT count(*) FROM act_hi_procinst WHERE super_process_instance_id_ IS NULL",
        timeout_ms=120_000)

    # Sequence variety: what is counted once cannot exceed what is counted at all.
    assert land.sequences_unique_once <= land.sequences_distinct
    if land.sequences_distinct:
        assert 0.0 < (land.sequence_concentration or 0) <= 1.0

    # Transitions: rare ones are NOT filtered away
    assert any(t.count == 1 for t in land.transitions), "rare transitions have to survive"

    # Co-occurrence is a different thing from the call graph
    without_call = [c for c in land.co_occurrence if not c.also_calls]
    assert without_call, "there have to be pairs that co-occur without calling each other"

    # Distributions instead of averages
    assert land.instances_per_key.p50 is not None
    assert land.gaps_ms.p50 is not None

    # Honesty
    assert any("not a process model" in n for n in m.notes)
    assert land.actors.start_users_available is False


@pytest.mark.integration
def test_the_drilldown_yields_the_same_numbers_as_the_aggregate(db, profile, landscape):
    """A number you cannot open is of little use -- and one that says something different when
    you open it is worse."""
    land = landscape
    t = next(t for t in land.transitions if 50 < t.count < 5000)
    keys, title, total = views_landscape._resolve_cases(db, L, "transition", t.from_def, t.to_def,
                                                 "", "", 500)
    assert total == t.keys, f"{t.from_def} -> {t.to_def}: aggregate {t.keys}, drill-down {total}"
    assert keys and t.from_def in title

    c = next(c for c in land.co_occurrence if 5 < c.keys < 2000)
    _keys, _title, total_co = views_landscape._resolve_cases(db, L, "cooccurrence", c.def_a, c.def_b,
                                                      "", "", 500)
    assert total_co == c.keys

    s = next(s for s in land.sequences if 5 < s.count < 2000)
    _k, _t, total_seq = views_landscape._resolve_cases(db, L, "sequence", "", "", "",
                                                ">".join(s.sequence), 500)
    assert total_seq == s.count


@pytest.mark.integration
def test_the_landscape_page_states_its_caveats(client):
    r = client.get(f"/profile/{PROFILE}/landscape")
    assert r.status_code == 200
    if "has not been computed" in r.text:
        pytest.skip("landscape not precomputed for this profile yet")
    assert "not a process model" in r.text, "this caveat belongs on the page"
    assert "median" in r.text.lower()
    assert "average of" not in r.text.lower()
    # The word "average" may only appear in a negation -- as the label of a number it would be a
    # claim this tool must not make.
    at = 0
    while True:
        at = r.text.find("average", at)
        if at < 0:
            break
        env = r.text[max(0, at - 30):at]
        assert any(w in env for w in ("never", "not as", "no ", "instead of")), (
            f"average used as a label: {r.text[max(0, at - 60):at + 30]!r}")
        at += 1
    assert "Sequence variety" in r.text and "Co-occurrence" in r.text
