"""Tests for ``cib7explorer.db.detect``.

The unit tests below run without a database. The integration test at the bottom needs a reachable
engine database (see tests/conftest.py) and is marked `@pytest.mark.integration`, so that
`pytest -m "not integration"` leaves it out.
"""

from __future__ import annotations

import os

import pytest

from cib7explorer.contracts import Feature, HistoryLevel
from cib7explorer.db import detect, sqlguard


# -- natural version ordering --------------------------------------------------------

def test_version_sort_key_orders_numerically_not_lexically():
    # Lexicographically '7.24.0' < '7.9.0' (because '2' < '9' as a character) -- which is exactly
    # the mistake version_sort_key has to avoid.
    assert detect.version_sort_key("7.9.0") < detect.version_sort_key("7.24.0")
    assert sorted(["7.24.0", "7.9.0", "7.15.0"], key=detect.version_sort_key) == [
        "7.9.0", "7.15.0", "7.24.0",
    ]


def test_highest_version_picks_the_natural_maximum():
    versions = ["7.15.0", "7.16.0", "7.17.0", "7.18.0", "7.19.0", "7.20.0",
                "7.21.0", "7.22.0", "7.23.0", "7.24.0"]
    assert detect.highest_version(versions) == "7.24.0"
    assert detect.highest_version([]) is None
    assert detect.highest_version(["7.9.0"]) == "7.9.0"


# -- History Level --------------------------------------------------------------------------

def test_history_level_parse():
    assert HistoryLevel.parse("2") is HistoryLevel.AUDIT
    assert HistoryLevel.parse(2) is HistoryLevel.AUDIT
    assert HistoryLevel.parse("3") is HistoryLevel.FULL
    assert HistoryLevel.parse(None) is None
    assert HistoryLevel.parse("not-a-number") is None
    assert HistoryLevel.parse("99") is None  # not a known level


# -- comparing fingerprints ------------------------------------------------------------------

def test_compare_fingerprint_detects_missing_table():
    expected = {"act_hi_procinst": ["id_:character varying", "start_time_:timestamp without time zone"]}
    actual: dict[str, dict[str, str]] = {}
    deviations = detect.compare_fingerprint(expected, actual)
    assert [d.kind for d in deviations] == ["missing_table"]
    assert deviations[0].table == "act_hi_procinst"


def test_compare_fingerprint_detects_missing_column_and_type_change_and_extra_column():
    expected = {
        "act_hi_procinst": [
            "id_:character varying",
            "start_time_:timestamp without time zone",
            "duration_:bigint",
        ]
    }
    actual = {
        "act_hi_procinst": {
            "id_": "character varying",
            # start_time_ is absent entirely -> missing_column
            "duration_": "integer",          # a different type -> type_changed (instead of bigint)
            "extra_flag_": "boolean",        # a column not in the reference -> extra_column
        }
    }
    deviations = detect.compare_fingerprint(expected, actual)
    kinds = sorted(d.kind for d in deviations)
    assert kinds == ["extra_column", "missing_column", "type_changed"]

    by_kind = {d.kind: d for d in deviations}
    assert "start_time_" in by_kind["missing_column"].detail
    assert "duration_" in by_kind["type_changed"].detail
    assert "bigint" in by_kind["type_changed"].detail and "integer" in by_kind["type_changed"].detail
    assert "extra_flag_" in by_kind["extra_column"].detail


def test_compare_fingerprint_matching_schema_yields_no_deviations():
    expected = {"act_ge_property": ["name_:character varying", "value_:character varying"]}
    actual = {"act_ge_property": {"name_": "character varying", "value_": "character varying"}}
    assert detect.compare_fingerprint(expected, actual) == []


# -- how the reason text is built ----------------------------------------------------------------------

def test_feature_reason_missing_table():
    reason = detect.feature_reason(
        Feature.HISTORIC_INCIDENTS, "act_hi_incident",
        exists=False, has_rows=False, required_level=HistoryLevel.FULL,
        history_level=HistoryLevel.AUDIT, history_level_raw="2",
    )
    assert "act_hi_incident" in reason
    assert "does not exist" in reason


def test_feature_reason_full_level_required_but_audit_configured():
    reason = detect.feature_reason(
        Feature.VARIABLE_UPDATES, "act_hi_detail",
        exists=True, has_rows=False, required_level=HistoryLevel.FULL,
        history_level=HistoryLevel.AUDIT, history_level_raw="2",
    )
    assert reason.startswith("act_hi_detail is empty")
    assert "history level FULL" in reason
    assert "AUDIT (2)" in reason


def test_feature_reason_unknown_history_level():
    reason = detect.feature_reason(
        Feature.JOB_LOG, "act_hi_job_log",
        exists=True, has_rows=False, required_level=HistoryLevel.FULL,
        history_level=None, history_level_raw="unknown-42",
    )
    # The raw value has to be echoed, so that an unknown level can be diagnosed at all.
    assert "unknown-42" in reason


def test_feature_reason_level_independent_empty_table():
    reason = detect.feature_reason(
        Feature.OPEN_INCIDENTS, "act_ru_incident",
        exists=True, has_rows=False, required_level=None,
        history_level=HistoryLevel.AUDIT, history_level_raw="2",
    )
    assert reason == "act_ru_incident is empty -- it holds no rows."


def test_feature_reason_bpmn_resources_specific_text():
    reason = detect.feature_reason(
        Feature.BPMN_RESOURCES, "act_ge_bytearray",
        exists=True, has_rows=False, required_level=None,
        history_level=HistoryLevel.AUDIT, history_level_raw="2",
    )
    assert "type_ = 1" in reason


def test_feature_reason_available_has_no_reason():
    reason = detect.feature_reason(
        Feature.PROCESS_INSTANCES, "act_hi_procinst",
        exists=True, has_rows=True, required_level=None,
        history_level=HistoryLevel.AUDIT, history_level_raw="2",
    )
    assert reason == ""


# -- every SQL constant has to pass the guard ------------------------------------------------

def test_all_sql_constants_pass_sqlguard():
    sql_constants = {
        name: value for name, value in vars(detect).items()
        if name.startswith("_SQL_") and isinstance(value, str)
    }
    assert sql_constants, "no _SQL_* constants found in detect.py"

    for name, sql in sql_constants.items():
        candidate = sql.format(table="act_hi_procinst") if "{table}" in sql else sql
        try:
            sqlguard.check(candidate)
        except sqlguard.UnsafeQuery as exc:  # pragma: no cover - this path should never trigger
            pytest.fail(f"{name} does not pass sqlguard.check(): {exc}\nSQL: {candidate}")


def test_all_table_names_are_safe_identifiers():
    for table in detect.ALL_TABLES:
        assert detect._IDENT_RE.match(table), f"unsafe table name in ALL_TABLES: {table!r}"


# -- integration test, needs a real database -------------------------------------------------

@pytest.mark.integration
def test_detect_against_real_database(db):
    from cib7explorer import config

    profile_name = os.environ.get("CIB7_TEST_PROFILE", "demo-dump")
    profile = config.get_profile(profile_name)

    result = detect.detect(db, profile)

    # Server and session
    assert "PostgreSQL" in result.server_version
    assert result.database_name == "camunda"
    assert result.connected_as == "explorer_ro"
    assert result.session_is_read_only is True

    # Engine properties
    assert result.history_level is HistoryLevel.AUDIT
    assert result.history_level_raw == "2"
    assert result.installation_id == "08c4c183-5e30-4efc-8ddc-812a4365bd2d"

    # Engine schema version (natural ordering: 7.24.0 is the highest, not the
    # the lexicographically last one)
    assert result.engine_schema_version == "7.24.0"
    assert len(result.schema_log) == 10

    # Flyway (optional, but present in this database)
    assert len(result.flyway_migrations) == 9

    # Schema deviations: this database matches the fingerprint exactly
    assert result.deviations == []

    # Capabilities: six available, seven (all the FULL ones) not
    available = {s.feature for s in result.features if s.available}
    unavailable = {s.feature for s in result.features if not s.available}
    assert available == {
        Feature.PROCESS_INSTANCES, Feature.ACTIVITY_INSTANCES, Feature.TASK_INSTANCES,
        Feature.VARIABLE_INSTANCES, Feature.OPEN_INCIDENTS, Feature.BPMN_RESOURCES,
    }
    assert unavailable == {
        Feature.VARIABLE_UPDATES, Feature.HISTORIC_INCIDENTS, Feature.IDENTITY_LINKS,
        Feature.OPERATION_LOG, Feature.JOB_LOG, Feature.EXTERNAL_TASK_LOG,
        Feature.DECISION_INSTANCES,
    }
    # every unavailable FULL capability has to name the configured level in its reason
    for status in result.features:
        if not status.available and status.table_exists and status.feature is not Feature.BPMN_RESOURCES:
            assert "AUDIT (2)" in status.reason or "holds no rows" in status.reason

    # The history window
    hw = result.history_window
    assert hw is not None
    assert hw.first_start.isoformat(timespec="milliseconds") == "2025-02-13T09:09:55.383"
    assert hw.last_start.isoformat(timespec="milliseconds") == "2026-08-18T12:03:38.467"
    assert hw.last_end.isoformat(timespec="milliseconds") == "2026-08-18T12:26:13.871"
    assert hw.running_instances == 5172
    assert hw.removal_time_min.isoformat(timespec="milliseconds") == "2026-08-18T07:44:30.541"
    assert hw.removal_time_max.isoformat(timespec="milliseconds") == "2028-02-09T12:26:13.871"
    # rows_past_removal_time grows with wall-clock time (removal_time_ < now()) -- hence a lower
    # bound rather than an exact value.
    assert hw.rows_past_removal_time is not None and hw.rows_past_removal_time >= 21
    assert hw.instances_without_removal_time == 10540

    # Tenants (shown for information only)
    assert result.tenant_ids == ["default"]

    # Evidence for the time zone
    tz = result.timezone
    assert tz is not None
    assert tz.db_timezone_setting == "UTC"
    assert tz.lag_to_db_now_seconds is not None
    assert tz.start_hour_histogram  # not empty

    assert result.detected_at is not None
    assert result.duration_ms is not None and result.duration_ms >= 0
