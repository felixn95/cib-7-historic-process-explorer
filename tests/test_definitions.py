"""Tests for ``cib7explorer.db.definitions``.

The unit tests below run without a database. The integration block at the bottom needs a
reachable engine database (see tests/conftest.py) and is marked ``@pytest.mark.integration``.
"""

from __future__ import annotations

import time

import pytest

from cib7explorer.config import Profile
from cib7explorer.contracts import DetectionResult, Classification, Feature, FeatureStatus
from cib7explorer.db import definitions, sqlguard


def _profile(**overrides) -> Profile:
    return Profile(name="test-profile", **overrides)


def _detection(*, historic_incidents_available: bool) -> DetectionResult:
    return DetectionResult(
        profile_name="test-profile",
        classification=Classification.TEST,
        server_version="PostgreSQL 16",
        database_name="camunda",
        connected_as="explorer_ro",
        session_is_read_only=True,
        installation_id=None,
        engine_schema_version=None,
        features=[
            FeatureStatus(
                feature=Feature.HISTORIC_INCIDENTS,
                available=historic_incidents_available,
                table="act_hi_incident",
                table_exists=historic_incidents_available,
                has_rows=historic_incidents_available,
                est_rows=None,
            ),
        ],
    )


# -- classify_end_activity: a pure function, with real end event names ---------------------

def test_classify_end_activity_validation_only_names():
    profile = _profile()
    assert definitions.classify_end_activity("EndEvent_ValidationOnly", profile) == (True, True)
    assert definitions.classify_end_activity("end_With_Validation_Only", profile) == (True, True)


def test_classify_end_activity_validation_related_but_not_only():
    profile = _profile()
    assert definitions.classify_end_activity("EndEvent_ValidationFailed", profile) == (False, True)
    assert definitions.classify_end_activity("EndEvent_IA_validation_failed", profile) == (False, True)


def test_classify_end_activity_unrelated_and_missing():
    profile = _profile()
    assert definitions.classify_end_activity("Event_1qwl7cn", profile) == (False, False)
    assert definitions.classify_end_activity(None, profile) == (False, False)


def test_classify_end_activity_empty_string_like_missing():
    profile = _profile()
    assert definitions.classify_end_activity("", profile) == (False, False)


def test_classify_end_activity_uses_profile_patterns_not_hardcoded_names():
    # The pattern list is editable -- a profile with its own patterns has to drive the
    # classification, without touching the code.
    profile = _profile(
        validation_only_patterns=(r"^only_check$",),
        validation_result_patterns=(r"check",),
    )
    assert definitions.classify_end_activity("only_check", profile) == (True, True)
    assert definitions.classify_end_activity("EndEvent_ValidationOnly", profile) == (False, False)


# -- every SQL constant has to pass the guard ------------------------------------------------

def test_all_sql_constants_pass_sqlguard():
    sql_constants = {
        name: value for name, value in vars(definitions).items()
        if name.startswith("_SQL_") and isinstance(value, str)
    }
    assert sql_constants, "no _SQL_* constants found in definitions.py"

    for name, sql in sql_constants.items():
        try:
            sqlguard.check(sql)
        except sqlguard.UnsafeQuery as exc:  # pragma: no cover - this path should never trigger
            pytest.fail(f"{name} does not pass sqlguard.check(): {exc}\nSQL: {sql}")


# -- assembly logic, no database -------------------------------------------------------------

def _inst_row(**overrides) -> dict:
    row = dict(
        instances=0, versions_used=0, completed=0, externally_terminated=0,
        internally_terminated=0, active=0, state_other=0, instances_as_root=0,
        instances_as_child=0, first_start=None, last_start=None, last_end=None,
        distinct_business_keys=0, instances_without_business_key=0,
    )
    row.update(overrides)
    return row


def _dep_row(**overrides) -> dict:
    row = dict(deployed_versions=1, latest_deployed_version=1, suspended_versions=0, latest_name=None)
    row.update(overrides)
    return row


def test_build_summary_key_with_instances_but_not_deployed():
    # (a) instances present, but no row in act_re_procdef -> deployed=False.
    summary = definitions._build_summary(
        "orphan-key", _profile(),
        inst=_inst_row(instances=50, instances_as_root=50, completed=50),
        dep=None, dur=None, end_rows=[],
        open_incidents=None, task_row=None, historic_incidents=None,
    )
    assert summary.deployed is False
    assert summary.instances == 50
    assert summary.deployed_versions is None
    assert summary.name is None


def test_build_summary_deployed_key_without_instances():
    # (b) a row in act_re_procdef, but no instances -> instances=0, deployed=True.
    summary = definitions._build_summary(
        "never-run-key", _profile(),
        inst=None,
        dep=_dep_row(deployed_versions=3, latest_deployed_version=3, latest_name="Never ran"),
        dur=None, end_rows=[],
        open_incidents=None, task_row=None, historic_incidents=None,
    )
    assert summary.instances == 0
    assert summary.deployed is True
    assert summary.deployed_versions == 3
    assert summary.latest_deployed_version == 3
    assert summary.name == "Never ran"
    assert summary.duration.n == 0
    assert summary.duration.n_unfinished == 0


def test_build_summary_open_incidents_none_vs_zero_is_preserved():
    # open_incidents is decided by the caller (None = the table could not be queried at all,
    # 0 = queried but no row for this definition) -- _build_summary only passes it through.
    with_none = definitions._build_summary(
        "k", _profile(), inst=_inst_row(instances=5), dep=None, dur=None, end_rows=[],
        open_incidents=None, task_row=None, historic_incidents=None,
    )
    with_zero = definitions._build_summary(
        "k", _profile(), inst=_inst_row(instances=5), dep=None, dur=None, end_rows=[],
        open_incidents=0, task_row=None, historic_incidents=None,
    )
    assert with_none.open_incidents is None
    assert with_zero.open_incidents == 0


def test_build_summary_user_task_fields_from_task_row():
    summary = definitions._build_summary(
        "k", _profile(), inst=_inst_row(instances=5), dep=None, dur=None, end_rows=[],
        open_incidents=None, task_row={"n": 7, "distinct_assignees": 2}, historic_incidents=None,
    )
    assert summary.user_task_instances == 7
    assert summary.distinct_assignees == 2

    # A missing row from a query that ran is a real 0, not "not recorded".
    summary_zero = definitions._build_summary(
        "k", _profile(), inst=_inst_row(instances=5), dep=None, dur=None, end_rows=[],
        open_incidents=None, task_row=None, task_data_available=True, historic_incidents=None,
    )
    assert summary_zero.user_task_instances == 0

    # "not recorded" only when the query itself was impossible.
    summary_none = definitions._build_summary(
        "k", _profile(), inst=_inst_row(instances=5), dep=None, dur=None, end_rows=[],
        open_incidents=None, task_row=None, task_data_available=False, historic_incidents=None,
    )
    assert summary_none.user_task_instances is None
    assert summary_none.distinct_assignees is None


def test_build_summary_duration_stats_and_n_unfinished():
    dur = dict(n=90, minimum=10, maximum=9999, p25=100, p50=200, p75=300, p90=400, p99=500)
    summary = definitions._build_summary(
        "k", _profile(), inst=_inst_row(instances=100), dep=None, dur=dur, end_rows=[],
        open_incidents=None, task_row=None, historic_incidents=None,
    )
    assert summary.duration.n == 90
    assert summary.duration.n_unfinished == 10       # 100 instances - 90 with duration_
    assert summary.duration.p50 == 200


def test_build_summary_end_activities_sorted_and_classified():
    end_rows = [
        {"act_id": "Event_1qwl7cn", "n": 5},
        {"act_id": "EndEvent_ValidationOnly", "n": 50},
        {"act_id": "EndEvent_ValidationFailed", "n": 20},
        {"act_id": None, "n": 3},
    ]
    summary = definitions._build_summary(
        "k", _profile(), inst=_inst_row(instances=78), dep=None, dur=None, end_rows=end_rows,
        open_incidents=None, task_row=None, historic_incidents=None,
    )
    assert [e.instances for e in summary.end_activities] == [50, 20, 5, 3]
    by_id = {e.act_id: e for e in summary.end_activities}
    assert by_id["EndEvent_ValidationOnly"].validation_only is True
    assert by_id["EndEvent_ValidationOnly"].validation_related is True
    assert by_id["EndEvent_ValidationFailed"].validation_only is False
    assert by_id["EndEvent_ValidationFailed"].validation_related is True
    assert by_id[None].validation_only is False
    assert by_id[None].validation_related is False
    assert summary.validation_only_instances == 50
    assert summary.validation_related_instances == 70


# -- historic_incidents: the actual honesty requirement -------------------------------------

def test_historic_incidents_value_none_without_detection():
    assert definitions._historic_incidents_value(None) is None


def test_historic_incidents_value_none_when_feature_unavailable():
    detection = _detection(historic_incidents_available=False)
    assert definitions._historic_incidents_value(detection) is None


def test_historic_incidents_value_zero_when_feature_available():
    detection = _detection(historic_incidents_available=True)
    assert definitions._historic_incidents_value(detection) == 0


# -- derived properties: only_as_child / only_as_root / both_roles ---------------------

def test_only_as_child_only_as_root_both_roles():
    only_child = definitions._build_summary(
        "k", _profile(), inst=_inst_row(instances=10, instances_as_root=0, instances_as_child=10),
        dep=None, dur=None, end_rows=[], open_incidents=None, task_row=None, historic_incidents=None,
    )
    assert only_child.only_as_child is True
    assert only_child.only_as_root is False
    assert only_child.both_roles is False

    only_root = definitions._build_summary(
        "k", _profile(), inst=_inst_row(instances=10, instances_as_root=10, instances_as_child=0),
        dep=None, dur=None, end_rows=[], open_incidents=None, task_row=None, historic_incidents=None,
    )
    assert only_root.only_as_root is True
    assert only_root.only_as_child is False
    assert only_root.both_roles is False

    both = definitions._build_summary(
        "k", _profile(), inst=_inst_row(instances=10, instances_as_root=4, instances_as_child=6),
        dep=None, dur=None, end_rows=[], open_incidents=None, task_row=None, historic_incidents=None,
    )
    assert both.both_roles is True
    assert both.only_as_root is False
    assert both.only_as_child is False

    no_instances = definitions._build_summary(
        "k", _profile(), inst=None, dep=_dep_row(), dur=None, end_rows=[],
        open_incidents=None, task_row=None, historic_incidents=None,
    )
    assert no_instances.only_as_child is False
    assert no_instances.only_as_root is False
    assert no_instances.both_roles is False


# -- integration tests, these need a real database -------------------------------------------

@pytest.mark.integration
def test_fetch_definitions_against_real_database(db):
    from cib7explorer import config
    from cib7explorer.db import detect
    import os

    profile_name = os.environ.get("CIB7_TEST_PROFILE", "demo-dump")
    profile = config.get_profile(profile_name)
    detection = detect.detect(db, profile)

    started = time.perf_counter()
    summaries = definitions.fetch_definitions(db, profile, detection=detection)
    duration_ms = int((time.perf_counter() - started) * 1000)
    print(f"\nfetch_definitions total runtime: {duration_ms} ms")

    # Every total is checked against the database itself rather than against numbers written
    # down once. That way the test says something on any installation -- and it says more: it
    # compares the aggregate with its own source instead of with a memory of it.
    assert len(summaries) == db.scalar("SELECT count(DISTINCT key_) FROM act_re_procdef")

    # Both history totals in one pass -- separately they are two scans for one answer.
    history = db.fetch("""
        SELECT count(*)                            AS rows_total,
               count(DISTINCT proc_def_key_)       AS keys_total
          FROM act_hi_procinst
    """, limit=1).one
    assert sum(s.instances for s in summaries) == history["rows_total"], (
        "the sum over all definitions has to be the number of history rows")

    with_instances = [s for s in summaries if s.instances > 0]
    never_run_but_deployed = [s for s in summaries if s.instances == 0 and s.deployed]
    assert len(with_instances) + len(never_run_but_deployed) == len(summaries), (
        "every definition is either one that ran or one that is deployed and never ran")
    assert len(with_instances) == history["keys_total"]

    # Roles: each instance is counted exactly once, as a root or as a child.
    for s in summaries:
        assert s.instances_as_root + s.instances_as_child == s.instances, s.key

    # The largest definition, whatever it is called on this installation.
    largest = summaries[0]
    assert largest.instances > 0
    counted = db.scalar(
        "SELECT count(*) FROM act_hi_procinst WHERE proc_def_key_ = %s", (largest.key,))
    assert largest.instances == counted
    assert largest.distinct_business_keys is None or largest.distinct_business_keys >= 0
    if largest.duration.p50 is not None:
        assert largest.duration.n + largest.duration.n_unfinished == largest.instances
        assert largest.duration.n_unfinished == largest.active

    # Ordering: descending by instance count, definitions that never ran at the end.
    instance_counts = [s.instances for s in summaries]
    assert instance_counts == sorted(instance_counts, reverse=True)
    if never_run_but_deployed:
        assert all(s.instances == 0 for s in summaries[-len(never_run_but_deployed):])

    # Open incidents in total (act_ru_incident, joined via act_re_procdef).
    total_open_incidents = sum(s.open_incidents for s in summaries if s.open_incidents is not None)
    incident_rows = db.scalar(
        "SELECT count(*) FROM act_ru_incident i "
        "JOIN act_re_procdef d ON d.id_ = i.proc_def_id_")
    assert total_open_incidents == incident_rows

    # End activities: their instance counts must not exceed the definition's own count.
    for s in summaries:
        for e in s.end_activities:
            assert e.instances <= s.instances, f"{s.key}/{e.act_id}"


@pytest.mark.integration
def test_fetch_versions_against_real_database(db, busiest_def_key):
    # The breakdown counts instances per version, so the subject has to be a definition that ran.
    # A deployed-but-never-run definition legitimately has no rows here; the overview list is
    # where its deployment state is reported.
    key = busiest_def_key
    versions = definitions.fetch_versions(db, key)
    assert len(versions) > 0
    # descending by version
    numbered = [v.version for v in versions if v.version is not None]
    assert numbered == sorted(numbered, reverse=True)
    assert all(v.key == key for v in versions)


@pytest.mark.integration
def test_fetch_versions_unknown_key_returns_empty(db):
    assert definitions.fetch_versions(db, "does-not-exist-at-all-12345") == []


def test_user_task_zero_is_a_real_zero_but_missing_query_is_not():
    """A definition without a row in act_hi_taskinst has 0 user tasks -- a real zero.
    "not recorded" may only appear when the query itself was impossible."""
    d = definitions

    present = d._build_summary(
        "order-8000", _profile(), inst=None, dep=None, dur=None, end_rows=[],
        open_incidents=None, task_row=None, task_data_available=True, historic_incidents=None,
    )
    assert present.user_task_instances == 0
    assert present.distinct_assignees == 0

    not_recorded = d._build_summary(
        "order-8000", _profile(), inst=None, dep=None, dur=None, end_rows=[],
        open_incidents=None, task_row=None, task_data_available=False, historic_incidents=None,
    )
    assert not_recorded.user_task_instances is None
    assert not_recorded.distinct_assignees is None


def test_validation_marker_both_spellings_and_undecidable():
    """The ``onlyValidation`` marker is written as a boolean by some processes and as a string by
    others. The SQL has to merge both -- and report a value that fits neither form as
    "undecidable" instead of silently as false."""
    sql = definitions._SQL_VALIDATION_FLAG
    assert "var_type_ = 'boolean'" in sql and "long_ = 1" in sql
    assert "var_type_ = 'string'" in sql and "lower(text_) = 'true'" in sql
    assert "flag_undecidable" in sql
    # The decisive restriction: only the input parameter at instance start counts.
    # Without it, every instance of a definition got counted as a dry run, when in fact only a
    # part of them were -- the rest came from an occurrence set inside the process, where the same
    # variable means something else.
    assert "act_inst_id_ = proc_inst_id_" in sql
    assert "flag_not_at_start" in sql, "instances without the input parameter must be reported apart"
    sqlguard.check(sql)


def test_validation_share_and_missing_determination():
    with_flag = definitions._build_summary(
        "order-3000", _profile(), inst=_inst_row(instances=1600), dep=None, dur=None,
        end_rows=[], open_incidents=None, task_row=None,
        validation_row={"flag_true": 1200, "flag_false": 400, "flag_undecidable": 0,
                        "flag_not_at_start": 0},
        historic_incidents=None,
    )
    assert with_flag.validation_flag_true == 1200
    assert with_flag.validation_flag_share == pytest.approx(1200 / 1600)

    without_flag = definitions._build_summary(
        "order-3000", _profile(), inst=_inst_row(instances=1600), dep=None, dur=None,
        end_rows=[], open_incidents=None, task_row=None, validation_row=None,
        historic_incidents=None,
    )
    assert without_flag.validation_flag_true is None, "not determined must not appear as 0"
    assert without_flag.validation_flag_share is None


@pytest.mark.integration
def test_validation_marker_against_a_real_database(db, profile):
    """Pins the corrected rule: only the input parameter at instance start counts.

    The first version counted "any occurrence is true" and reported nearly three times as many
    validation-only instances as there are, because some processes set the same variable again at
    an activity inside the run. The check below therefore compares the module's number with a
    query that applies the restriction explicitly -- if anybody drops it, the two disagree.
    """
    defs = definitions.fetch_definitions(db, profile)
    flagged = [d for d in defs if d.validation_flag_true is not None]
    if not flagged:
        pytest.skip("no validation marker variable in this installation")

    total_true = sum(d.validation_flag_true or 0 for d in flagged)
    total_false = sum(d.validation_flag_false or 0 for d in flagged)

    # The same rule, formulated independently: the restriction to instance start lives in the
    # WHERE clause here, not in a FILTER clause. Drop it from the module and the two disagree --
    # which is precisely the regression this test exists for.
    independent = db.fetch(
        """
        SELECT count(*) FILTER (WHERE flag)     AS n_true,
               count(*) FILTER (WHERE NOT flag) AS n_false
          FROM (
                SELECT proc_inst_id_,
                       bool_and(
                           CASE WHEN var_type_ = 'boolean' AND long_ = 1             THEN true
                                WHEN var_type_ = 'boolean' AND long_ = 0             THEN false
                                WHEN var_type_ = 'string' AND lower(text_) = 'true'  THEN true
                                WHEN var_type_ = 'string' AND lower(text_) = 'false' THEN false
                           END
                       ) AS flag
                  FROM act_hi_varinst
                 WHERE name_ = %s
                   AND proc_def_key_ IS NOT NULL
                   AND act_inst_id_ = proc_inst_id_
                 GROUP BY 1
               ) per_instance
         WHERE flag IS NOT NULL
        """,
        (definitions.VALIDATION_FLAG_VARIABLE,), limit=1)
    if independent.rows:
        n_true, n_false = independent.rows[0]
        assert total_true == n_true, (
            "a higher number means occurrences from inside the process were counted too")
        assert total_false == n_false

    # Counted instances never exceed the definition's own instances, and no share exceeds 100 %.
    for d in flagged:
        assert (d.validation_flag_true or 0) + (d.validation_flag_false or 0) <= d.instances, d.key
        share = d.validation_flag_share
        assert share is None or share <= 1.0, f"{d.key}: share {share}"
