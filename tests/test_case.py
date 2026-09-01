"""Tests for the case: closure, origin, gaps.

The most important integration test here is ``test_the_closure_matches_the_recursive_counter_check``:
the fast route over ``root_proc_inst_id_`` is two orders of magnitude quicker but relies on that
field being populated throughout. If that does not hold on some other database, this test has to
say so -- rather than the view quietly showing half a timeline.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from cib7explorer.contracts import GapKind, InstanceOrigin, TreeSummary
from cib7explorer.db import sqlguard, case as V

T0 = datetime(2026, 8, 3, 10, 0, 0)


def _tree(minute_start: int, minute_end: int | None, *, root: str = "", def_key: str = "p",
          running: bool = False) -> TreeSummary:
    return TreeSummary(
        root_id=root or f"r{minute_start}",
        def_key=def_key,
        start_time=T0 + timedelta(minutes=minute_start),
        end_time=None if minute_end is None else T0 + timedelta(minutes=minute_end),
        instance_count=1, max_depth=0, running=running or minute_end is None,
    )


# --- SQL --------------------------------------------------------------------------------

def test_every_sql_constant_passes_the_guard():
    n = 0
    for name, val in vars(V).items():
        if name.startswith("_SQL_") and isinstance(val, str):
            sqlguard.check(val)
            n += 1
    assert n >= 10


def test_the_closure_uses_root_and_the_counter_check_uses_recursion():
    assert "root_proc_inst_id_ IN (" in V._SQL_CLOSURE
    assert "RECURSIVE" not in V._SQL_CLOSURE, "the fast variant must not be recursive"
    assert "RECURSIVE" in V._SQL_CLOSURE_RECURSIVE


def test_validation_marker_only_at_instance_start():
    """The same trap as on the definition pages: the value set inside the process means something
    else entirely."""
    assert "act_inst_id_ = proc_inst_id_" in V._SQL_VALIDATION_PER_INSTANCE


def test_start_activity_joins_on_start_act_id_and_not_on_the_type():
    """A type filter would count the start events of embedded subprocesses as well -- a real case
    can hold more start events than instances."""
    assert "a.act_id_ = p.start_act_id_" in V._SQL_START_ACTIVITY
    assert "act_type_ LIKE" not in V._SQL_START_ACTIVITY


# --- Gaps ----------------------------------------------------------------------------

def test_gap_between_two_trees():
    gaps, open_ids = V._build_gaps([_tree(0, 10), _tree(30, 40)])
    assert open_ids is False
    assert len(gaps) == 1
    g = gaps[0]
    assert g.kind is GapKind.BETWEEN
    assert g.duration_ms == 20 * 60 * 1000

def test_an_overlap_is_not_a_gap():
    gaps, _ = V._build_gaps([_tree(0, 30), _tree(10, 20)])
    assert [g.kind for g in gaps] == [GapKind.OVERLAP]
    assert gaps[0].duration_ms == 20 * 60 * 1000


def test_high_water_mark_instead_of_the_immediate_predecessor():
    """A short tree in the middle of a long one must not produce a gap: the comparison is against
    the high-water mark of all previous end times, not against the predecessor in the list."""
    trees = [_tree(0, 120, root="long"), _tree(10, 20, root="short"), _tree(60, 70, root="short2")]
    gaps, _ = V._build_gaps(trees)
    assert all(g.kind is GapKind.OVERLAP for g in gaps), [g.kind for g in gaps]
    assert not any(g.kind is GapKind.BETWEEN for g in gaps)


def test_a_running_tree_prevents_gaps_after_it():
    """While something is running there is no period without a process -- and therefore no gap."""
    gaps, open_ids = V._build_gaps([_tree(0, None, root="running"), _tree(100, 110)])
    assert open_ids is True
    assert [g.kind for g in gaps] == [GapKind.OVERLAP]
    assert gaps[0].duration_ms is None


def test_no_gap_with_only_one_tree():
    gaps, open_ids = V._build_gaps([_tree(0, 5)])
    assert gaps == []
    assert open_ids is False


# --- start trigger --------------------------------------------------------------------------

def test_parent_process_takes_precedence_as_trigger():
    trigger, detail = V._derive_trigger(
        {"super_process_instance_id_": "abc", "restarted_proc_inst_id_": None},
        {"act_type_": "startEvent", "act_id_": "S1"})
    assert trigger.value == "parent_process"
    assert "call activity" in detail


@pytest.mark.parametrize("act_type,expected", [
    ("signalStartEvent", "signal"),
    ("messageStartEvent", "message"),
    ("timerStartEvent", "timer"),
    ("startEvent", "plain_start"),
])
def test_trigger_derived_from_the_start_activity(act_type, expected):
    trigger, _ = V._derive_trigger({"super_process_instance_id_": None},
                                   {"act_type_": act_type, "act_id_": "S1"})
    assert trigger.value == expected


def test_an_unknown_trigger_is_not_guessed_to_be_api():
    trigger, _ = V._derive_trigger({"super_process_instance_id_": None}, None)
    assert trigger.value == "unknown", "without evidence nothing may be claimed"


def test_absence_of_a_trigger_never_means_user():
    """start_user_id_ is NULL throughout -- "started by a user" must never come out of this."""
    for act_type in ("startEvent", "signalStartEvent", None):
        trigger, _ = V._derive_trigger({"super_process_instance_id_": None},
                                       {"act_type_": act_type, "act_id_": "x"} if act_type else None)
        assert trigger.value != "user"


# --- Integration ------------------------------------------------------------------------

@pytest.mark.integration
def test_a_real_case_end_to_end(db, profile, sample_business_key):
    """Every part of a loaded case has to add up -- stated as properties, not as remembered
    numbers, so that this says something on any installation."""
    v = V.load_case(db, profile, sample_business_key)
    assert v.instances_shown > 0
    assert v.instances_shown <= v.instances_total
    assert v.instances_shown <= V.MAX_INSTANCES

    # The three origin buckets partition the shown instances -- no instance in two, none lost.
    assert (v.instances_with_own_key + v.instances_without_key
            + v.instances_with_other_key) == v.instances_shown

    assert v.definitions, "a case with instances has at least one definition"
    # definitions is the set of definition keys the shown instances belong to.
    assert set(v.definitions) == {i.def_key for i in v.instances}
    assert len(v.definitions) <= v.instances_shown

    assert 0 < v.trees_total or v.instances_shown == 0
    assert v.validation_only_count <= v.instances_shown

    # A foreign key earns its name only by carrying instances outside this case; that is why it
    # is reported separately instead of being merged in.
    for f in v.foreign_keys:
        assert f.key != sample_business_key
        assert f.instances_outside > 0, f"{f.key} was reported without anything outside"


@pytest.mark.integration
def test_the_closure_matches_the_recursive_counter_check(db):
    # The keys come from the database: cases with several instances, because a single-instance
    # case cannot expose a broken closure -- but below the display limit, above which the
    # counter-check itself is bounded and returns "not determined".
    rows = db.fetch(
        "SELECT business_key_ FROM act_hi_procinst "
        "WHERE business_key_ IS NOT NULL AND business_key_ <> '' "
        "GROUP BY 1 HAVING count(*) BETWEEN 2 AND %s "
        "ORDER BY count(*) DESC, 1 LIMIT 3",
        (V.MAX_INSTANCES // 2,), limit=3)
    keys = [r[0] for r in rows.rows]
    if not keys:
        pytest.skip("no case with between two instances and half the display limit")
    # (-1, -1) means the check could not be carried out -- a statement different from "the two
    # disagree", so it must not be reported as a broken closure. But it must not silently take
    # the whole test with it either: under load a single key can time out, and skipping then
    # would quietly remove the check. So the next key is tried, and the test only gives up when
    # not one of them could be verified.
    verified = 0
    for key in keys:
        equal, via_root, recursive = V.verify_closure_equivalence(db, key)
        if (via_root, recursive) == (-1, -1):
            continue
        assert equal, (
            f"{key}: {via_root} via root, {recursive} recursive -- root_proc_inst_id_ is unreliable")
        assert via_root > 0
        verified += 1
    if not verified:
        pytest.skip(f"the counter-check could not be carried out for any of {keys}")


@pytest.mark.integration
def test_a_huge_case_is_bounded_and_says_so(db, profile):
    # A case that actually exceeds the display limit, looked up rather than remembered.
    key = db.scalar(
        "SELECT business_key_ FROM act_hi_procinst "
        "WHERE business_key_ IS NOT NULL AND business_key_ <> '' "
        "GROUP BY 1 HAVING count(*) > %s ORDER BY count(*) DESC LIMIT 1",
        (V.MAX_INSTANCES,))
    if not key:
        pytest.skip(f"no case with more than {V.MAX_INSTANCES} instances in this installation")
    v = V.load_case(db, profile, key)
    assert v.instances_shown == V.MAX_INSTANCES
    assert v.instances_total > v.instances_shown
    assert any(str(v.instances_total // 1000) in n.text or "are shown" in n.text
               for n in v.notes if n.level == "warn")


@pytest.mark.integration
def test_correlation_finds_instances_outside_the_case(db, profile, sample_business_key):
    v = V.load_case(db, profile, sample_business_key)
    cors = V.correlate(db, profile, v)
    if not cors:
        pytest.skip("no correlation variable of this profile links beyond this case")
    ids_in_case = {i.proc_inst_id for i in v.instances}
    for c in cors:
        assert c.instances_outside > 0, f"{c.variable} was reported without anything outside"
        assert not (set(c.outside_ids) & ids_in_case), (
            "the second track must not contain anything already inside the case")
