"""What the tool is actually talking to.

``detect()`` answers that question at connect time: engine and schema version, configured
history level, the period the history covers, approximate table sizes, and whether the schema
found differs from the reference shape in ``schema_fingerprint.json``.

Two principles run through the whole file:

* **Row counts are estimates** (``pg_class.reltuples``), never ``count(*)`` on a history table.
  An exact count is only considered when the estimate is below ``MAX_EXACT_COUNT_ROWS``. That
  rule applies on a test copy exactly as it does on a production database -- code that is
  careful only in production is code that was never tested being careful.
* **A partial finding may fail without taking detection down.** A missing table, a query that
  times out, anything: each step catches its own errors and records the reason in the result
  (as a ``reason`` text for features, as a log entry otherwise) instead of aborting the whole
  detection. Only a broken connection, established before this module runs, fails hard.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import Profile
from ..contracts import (
    DetectionResult,
    Feature,
    FeatureStatus,
    HistoryLevel,
    HistoryWindow,
    SchemaDeviation,
    TableInfo,
    TimezoneEvidence,
)
from .connection import Database, DatabaseError

log = logging.getLogger("cib7explorer.db.detect")

#: Above this estimated row count, no ``count(*)`` is run -- not even a filtered one, because
#: in the worst case a filtered count still reads the entire table. This affects the number of
#: running instances and the history-cleanup figures on ``act_hi_procinst``, which are then
#: reported as "not determined" rather than guessed.
MAX_EXACT_COUNT_ROWS = 2_000_000

#: The history level from which a table is written at all. ``None`` means independent of the
#: level (for instance ``act_ru_incident``, which reflects running state).
_FEATURE_TABLES: dict[Feature, tuple[str, HistoryLevel | None]] = {
    Feature.PROCESS_INSTANCES: ("act_hi_procinst", None),
    Feature.ACTIVITY_INSTANCES: ("act_hi_actinst", None),
    Feature.TASK_INSTANCES: ("act_hi_taskinst", None),
    Feature.VARIABLE_INSTANCES: ("act_hi_varinst", None),
    Feature.VARIABLE_UPDATES: ("act_hi_detail", HistoryLevel.FULL),
    Feature.HISTORIC_INCIDENTS: ("act_hi_incident", HistoryLevel.FULL),
    Feature.OPEN_INCIDENTS: ("act_ru_incident", None),
    Feature.IDENTITY_LINKS: ("act_hi_identitylink", HistoryLevel.FULL),
    Feature.OPERATION_LOG: ("act_hi_op_log", HistoryLevel.FULL),
    Feature.JOB_LOG: ("act_hi_job_log", HistoryLevel.FULL),
    Feature.EXTERNAL_TASK_LOG: ("act_hi_ext_task_log", HistoryLevel.FULL),
    Feature.DECISION_INSTANCES: ("act_hi_decinst", HistoryLevel.FULL),
    Feature.BPMN_RESOURCES: ("act_ge_bytearray", None),
}

#: Readable descriptions for the reason text. Only needed for level-dependent features;
#: level-independent ones get a simpler sentence. All phrased as plural subjects ("... are only
#: written from ..."), so the generated sentence reads correctly whichever feature is missing.
_FEATURE_DESCRIPTIONS: dict[Feature, str] = {
    Feature.VARIABLE_UPDATES: "variable updates",
    Feature.HISTORIC_INCIDENTS: "historic incidents",
    Feature.IDENTITY_LINKS: "identity links",
    Feature.OPERATION_LOG: "operation log entries",
    Feature.JOB_LOG: "job log entries",
    Feature.EXTERNAL_TASK_LOG: "external task log entries",
    Feature.DECISION_INSTANCES: "decision instances (DMN)",
}

_SCHEMA_FINGERPRINT_PATH = Path(__file__).with_name("schema_fingerprint.json")
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _load_fingerprint() -> dict[str, list[str]]:
    return json.loads(_SCHEMA_FINGERPRINT_PATH.read_text(encoding="utf-8"))


_FINGERPRINT: dict[str, list[str]] = _load_fingerprint()

#: Tables from the reference schema, plus the ones needed for features and the overview that
#: are not part of the fingerprint itself.
_EXTRA_TABLES: tuple[str, ...] = (
    "act_hi_comment", "act_hi_attachment", "act_hi_decinst", "act_hi_batch",
    "act_hi_caseinst", "act_hi_job_log", "act_hi_ext_task_log", "act_ru_task",
    "act_ru_variable", "flyway_schema_history",
)

ALL_TABLES: tuple[str, ...] = tuple(sorted(set(_FINGERPRINT) | set(_EXTRA_TABLES)))


# -- SQL constants -------------------------------------------------------------------------
# The test suite collects every constant in this module via vars(detect) and runs it through
# sqlguard.check(), so a statement that is not a plain read cannot be added here unnoticed.
# The binding check happens on every call inside Database.fetch() anyway.

_SQL_SESSION = """
    SELECT version() AS server_version,
           current_database() AS database_name,
           current_user AS connected_as,
           current_setting('TimeZone') AS db_tz,
           now() AS db_now
"""

_SQL_PROPERTIES = "SELECT name_, value_ FROM act_ge_property"

_SQL_SCHEMA_LOG = "SELECT id_, version_, timestamp_ FROM act_ge_schema_log ORDER BY timestamp_, id_"

_SQL_FLYWAY = "SELECT version, description, installed_on FROM flyway_schema_history ORDER BY installed_rank"

_SQL_TABLE_OVERVIEW = """
    SELECT c.relname AS table_name,
           c.reltuples::bigint AS est_rows,
           pg_total_relation_size(c.oid) AS total_bytes
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = %(schema)s AND c.relkind = 'r' AND c.relname = ANY(%(tables)s)
"""

#: Template for the cheap "does it hold anything" probe per table -- never a ``count(*)``. The
#: placeholder is only ever filled with a name from ``ALL_TABLES``, checked against a strict
#: identifier pattern, and never with anything that came from outside.
_SQL_HAS_ROWS_TEMPLATE = "SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1) AS has_rows"

_SQL_BPMN_RESOURCE_PROBE = "SELECT 1 AS found FROM act_ge_bytearray WHERE type_ = 1 LIMIT 1"

_SQL_HISTORY_MINMAX = (
    "SELECT min(start_time_) AS first_start, max(start_time_) AS last_start, "
    "max(end_time_) AS last_end FROM act_hi_procinst"
)

_SQL_RUNNING_COUNT = "SELECT count(*) AS n FROM act_hi_procinst WHERE end_time_ IS NULL"

_SQL_REMOVAL_MINMAX = (
    "SELECT min(removal_time_) AS removal_min, max(removal_time_) AS removal_max "
    "FROM act_hi_procinst"
)

_SQL_PAST_REMOVAL_COUNT = "SELECT count(*) AS n FROM act_hi_procinst WHERE removal_time_ < now()"

_SQL_NO_REMOVAL_COUNT = "SELECT count(*) AS n FROM act_hi_procinst WHERE removal_time_ IS NULL"

_SQL_TENANTS = "SELECT DISTINCT tenant_id_ FROM act_hi_procinst"

_SQL_START_HOUR_HISTOGRAM = """
    SELECT extract(hour FROM start_time_)::int AS h, count(*) AS n
      FROM act_hi_procinst
     WHERE start_time_ >= (SELECT max(start_time_) FROM act_hi_procinst) - interval '30 days'
     GROUP BY 1
     ORDER BY 1
"""

_SQL_COLUMNS = """
    SELECT table_name, column_name, data_type
      FROM information_schema.columns
     WHERE table_schema = %(schema)s AND table_name = ANY(%(tables)s)
"""


# -- small helpers that absorb partial failures ---------------------------------------------

def _safe_fetch(db: Database, sql: str, params: Any = None, *, limit: int, name: str):
    try:
        return db.fetch(sql, params, limit=limit, name=name)
    except DatabaseError as exc:
        log.warning("partial finding '%s' failed: %s", name, exc)
        return None


def _safe_one(db: Database, sql: str, params: Any = None, *, name: str) -> dict[str, Any] | None:
    r = _safe_fetch(db, sql, params, limit=1, name=name)
    return r.one if r else None


def _safe_scalar(db: Database, sql: str, params: Any = None, *, name: str) -> Any:
    r = _safe_fetch(db, sql, params, limit=1, name=name)
    if not r or not r.rows:
        return None
    return r.rows[0][0]


# -- natural version ordering ---------------------------------------------------------------

def version_sort_key(version: str) -> tuple[int, ...]:
    """'7.9.0' < '7.24.0': compare versions numerically per component, not lexicographically."""
    parts = re.findall(r"\d+", version or "")
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts)


def highest_version(versions: Iterable[str]) -> str | None:
    versions = [v for v in versions if v]
    if not versions:
        return None
    return max(versions, key=version_sort_key)


# -- fingerprint comparison (a pure function, testable without a database) ------------------

def compare_fingerprint(
    expected: dict[str, list[str]],
    actual: dict[str, dict[str, str]],
) -> list[SchemaDeviation]:
    """Compare the expected schema (``{table: ["column:type", ...]}``) against what was found
    (``{table: {column: type}}``).

    Missing tables or columns and changed types are serious (``missing_table``,
    ``missing_column``, ``type_changed``). Additional columns are merely worth mentioning
    (``extra_column``): they break nothing, but they are the visible trace of a customised or
    newer schema, and a reader interpreting the numbers should know about them.
    """
    deviations: list[SchemaDeviation] = []
    for table, cols in expected.items():
        exp_map = dict(c.split(":", 1) for c in cols)
        if table not in actual:
            deviations.append(SchemaDeviation(
                table=table, kind="missing_table",
                detail=f"table {table} is missing from the schema found.",
            ))
            continue
        act_map = actual[table]
        for col, col_type in exp_map.items():
            if col not in act_map:
                deviations.append(SchemaDeviation(
                    table=table, kind="missing_column",
                    detail=f"column {table}.{col} ({col_type}) is missing.",
                ))
            elif act_map[col] != col_type:
                deviations.append(SchemaDeviation(
                    table=table, kind="type_changed",
                    detail=f"{table}.{col}: expected {col_type}, found {act_map[col]}.",
                ))
        for col, col_type in act_map.items():
            if col not in exp_map:
                deviations.append(SchemaDeviation(
                    table=table, kind="extra_column",
                    detail=f"additional column {table}.{col} ({col_type}), not in the reference schema.",
                ))
    return deviations


# -- reason text for features ----------------------------------------------------------------

def feature_reason(
    feature: Feature,
    table: str,
    *,
    exists: bool,
    has_rows: bool,
    required_level: HistoryLevel | None,
    history_level: HistoryLevel | None,
    history_level_raw: str | None,
) -> str:
    """Build the reason why a feature is or is not available.

    The reason matters more than the yes/no: "empty because the history level does not write
    this table" and "empty because nothing happened yet" lead to entirely different conclusions.
    """
    if not exists:
        return f"{table} does not exist in this schema."
    if has_rows:
        return ""
    if feature is Feature.BPMN_RESOURCES:
        return f"{table} holds no resources of type BPMN (type_ = 1)."
    if required_level is not None:
        desc = _FEATURE_DESCRIPTIONS.get(feature, table)
        if history_level is not None:
            current = f"{history_level.label} ({history_level.value})"
        else:
            current = f"unknown (raw: {history_level_raw!r})"
        return (
            f"{table} is empty -- {desc} are only written from history level "
            f"{required_level.label} onwards, and {current} is configured."
        )
    return f"{table} is empty -- it holds no rows."


# -- individual steps ----------------------------------------------------------------------

def _detect_session(db: Database) -> dict[str, Any] | None:
    return _safe_one(db, _SQL_SESSION, name="session")


def _detect_properties(db: Database) -> dict[str, str]:
    r = _safe_fetch(db, _SQL_PROPERTIES, limit=200, name="ge_property")
    if not r:
        return {}
    return {row["name_"]: row["value_"] for row in r.dicts()}


def _detect_schema_log(db: Database) -> list[tuple[str, datetime | None]]:
    r = _safe_fetch(db, _SQL_SCHEMA_LOG, limit=500, name="schema_log")
    if not r:
        return []
    return [(row["version_"], row["timestamp_"]) for row in r.dicts()]


def _detect_flyway(db: Database) -> list[tuple[str, str, datetime | None]]:
    """The Flyway history is optional -- a missing table is a finding, not a failure."""
    r = _safe_fetch(db, _SQL_FLYWAY, limit=500, name="flyway")
    if not r:
        return []
    return [(row["version"], row["description"], row["installed_on"]) for row in r.dicts()]


def _detect_tables(db: Database) -> dict[str, TableInfo]:
    """Existence, size and occupancy for ``ALL_TABLES``.

    Existence follows from membership in the pg_class result -- a table that does not appear
    does not exist, so no separate ``to_regclass`` call is needed. Occupancy comes from a cheap
    EXISTS probe per existing table, never from a ``count(*)``.
    """
    overview = _safe_fetch(
        db, _SQL_TABLE_OVERVIEW, {"schema": db.profile.schema, "tables": list(ALL_TABLES)},
        limit=len(ALL_TABLES) + 1, name="table_overview",
    )
    sizes: dict[str, tuple[int | None, int | None]] = {}
    if overview:
        for row in overview.dicts():
            sizes[row["table_name"]] = (row["est_rows"], row["total_bytes"])

    infos: dict[str, TableInfo] = {}
    for table in ALL_TABLES:
        exists = table in sizes
        est_rows, total_bytes = sizes.get(table, (None, None))
        has_rows: bool | None = None
        if exists:
            assert _IDENT_RE.match(table), f"unsafe table name: {table!r}"
            sql = _SQL_HAS_ROWS_TEMPLATE.format(table=table)
            row = _safe_one(db, sql, name=f"has_rows_{table}")
            has_rows = bool(row["has_rows"]) if row else None
        infos[table] = TableInfo(
            name=table, exists=exists, est_rows=est_rows, total_bytes=total_bytes,
            has_rows=has_rows,
        )
    return infos


def _detect_history_window(db: Database, tables: dict[str, TableInfo]) -> HistoryWindow:
    info = tables.get("act_hi_procinst")
    if not info or not info.exists:
        log.info("act_hi_procinst is missing -- no history window can be determined")
        return HistoryWindow(
            first_start=None, last_start=None, last_end=None, running_instances=None,
            removal_time_min=None, removal_time_max=None, rows_past_removal_time=None,
            instances_without_removal_time=None,
        )

    minmax = _safe_one(db, _SQL_HISTORY_MINMAX, name="hist_minmax")
    removal = _safe_one(db, _SQL_REMOVAL_MINMAX, name="hist_removal_minmax")

    exact_ok = info.est_rows is not None and info.est_rows < MAX_EXACT_COUNT_ROWS
    running = past_removal = no_removal = None
    if exact_ok:
        running = _safe_scalar(db, _SQL_RUNNING_COUNT, name="hist_running")
        past_removal = _safe_scalar(db, _SQL_PAST_REMOVAL_COUNT, name="hist_past_removal")
        no_removal = _safe_scalar(db, _SQL_NO_REMOVAL_COUNT, name="hist_no_removal")
    else:
        log.info(
            "act_hi_procinst estimate %r is at or above the limit %d, or unknown -- running "
            "instances and cleanup figures are not counted exactly",
            info.est_rows, MAX_EXACT_COUNT_ROWS,
        )

    return HistoryWindow(
        first_start=minmax.get("first_start") if minmax else None,
        last_start=minmax.get("last_start") if minmax else None,
        last_end=minmax.get("last_end") if minmax else None,
        running_instances=running,
        removal_time_min=removal.get("removal_min") if removal else None,
        removal_time_max=removal.get("removal_max") if removal else None,
        rows_past_removal_time=past_removal,
        instances_without_removal_time=no_removal,
    )


def _detect_tenants(db: Database, tables: dict[str, TableInfo]) -> list[str | None]:
    info = tables.get("act_hi_procinst")
    if not info or not info.exists or not info.has_rows:
        return []
    r = _safe_fetch(db, _SQL_TENANTS, limit=50, name="tenants")
    if not r:
        return []
    return [row["tenant_id_"] for row in r.dicts()]


def _has_bpmn_resources(db: Database) -> bool:
    r = _safe_fetch(db, _SQL_BPMN_RESOURCE_PROBE, limit=1, name="bpmn_resource_probe")
    return bool(r and r.rows)


def _detect_features(
    db: Database,
    tables: dict[str, TableInfo],
    history_level: HistoryLevel | None,
    history_level_raw: str | None,
) -> list[FeatureStatus]:
    statuses: list[FeatureStatus] = []
    for feature in Feature:
        table, required_level = _FEATURE_TABLES[feature]
        info = tables.get(table)
        exists = bool(info and info.exists)
        has_rows = bool(info and info.has_rows)
        if feature is Feature.BPMN_RESOURCES and exists:
            has_rows = _has_bpmn_resources(db)
        available = exists and has_rows
        reason = feature_reason(
            feature, table, exists=exists, has_rows=has_rows,
            required_level=required_level, history_level=history_level,
            history_level_raw=history_level_raw,
        )
        statuses.append(FeatureStatus(
            feature=feature, available=available, table=table, table_exists=exists,
            has_rows=has_rows, est_rows=(info.est_rows if info else None), reason=reason,
        ))
    return statuses


def _detect_deviations(db: Database) -> list[SchemaDeviation]:
    tables = list(_FINGERPRINT)
    r = _safe_fetch(
        db, _SQL_COLUMNS, {"schema": db.profile.schema, "tables": tables},
        limit=5000, name="columns",
    )
    if not r:
        log.warning("schema comparison could not be read -- no deviations determined")
        return []
    actual: dict[str, dict[str, str]] = {}
    for row in r.dicts():
        actual.setdefault(row["table_name"], {})[row["column_name"]] = row["data_type"]
    return compare_fingerprint(_FINGERPRINT, actual)


def _detect_timezone(
    profile: Profile,
    session: dict[str, Any] | None,
    history: HistoryWindow,
    histogram_rows: list[dict[str, Any]],
) -> TimezoneEvidence:
    db_now = session.get("db_now") if session else None
    db_tz = session.get("db_tz") if session else None
    latest = history.last_start

    lag: float | None = None
    if db_now is not None and latest is not None:
        db_now_naive = db_now.replace(tzinfo=None) if db_now.tzinfo else db_now
        lag = (db_now_naive - latest).total_seconds()

    histogram = {int(row["h"]): int(row["n"]) for row in histogram_rows if row.get("h") is not None}

    return TimezoneEvidence(
        db_now=db_now,
        db_timezone_setting=db_tz,
        latest_history_timestamp=latest,
        lag_to_db_now_seconds=lag,
        start_hour_histogram=histogram,
        configured_source_timezone=profile.source_timezone,
        configured_display_timezone=profile.display_timezone,
    )


# -- entry point ----------------------------------------------------------------------------

def detect(db: Database, profile: Profile) -> DetectionResult:
    """Establish what this connection is talking to.

    No partial finding may let an exception escape: a failed step leaves empty or ``None``
    values plus a log entry, not a stack trace. Only a broken connection -- which happens
    before this function, in ``connection.connect()`` -- is allowed to fail hard.
    """
    started = time.perf_counter()

    session = _detect_session(db)
    properties = _detect_properties(db)
    schema_log = _detect_schema_log(db)
    flyway = _detect_flyway(db)
    tables = _detect_tables(db)

    history_level_raw = properties.get("historyLevel")
    history_level = HistoryLevel.parse(history_level_raw)
    installation_id = properties.get("camunda.installation.id")

    history_window = _detect_history_window(db, tables)
    tenant_ids = _detect_tenants(db, tables)
    features = _detect_features(db, tables, history_level, history_level_raw)
    deviations = _detect_deviations(db)

    histogram_result = _safe_fetch(db, _SQL_START_HOUR_HISTOGRAM, limit=30, name="start_hour_histogram")
    histogram_rows = histogram_result.dicts() if histogram_result else []
    timezone_evidence = _detect_timezone(profile, session, history_window, histogram_rows)

    engine_schema_version = highest_version(v for v, _ in schema_log)

    duration_ms = int((time.perf_counter() - started) * 1000)

    return DetectionResult(
        profile_name=profile.name,
        classification=profile.classification,
        server_version=(session or {}).get("server_version") or "",
        database_name=(session or {}).get("database_name") or "",
        connected_as=(session or {}).get("connected_as") or "",
        session_is_read_only=db.read_only_proof.ok,
        installation_id=installation_id,
        engine_schema_version=engine_schema_version,
        schema_log=schema_log,
        flyway_migrations=flyway,
        history_level=history_level,
        history_level_raw=history_level_raw,
        history_window=history_window,
        tables=list(tables.values()),
        features=features,
        deviations=deviations,
        tenant_ids=tenant_ids,
        timezone=timezone_evidence,
        detected_at=datetime.now(timezone.utc),
        duration_ms=duration_ms,
    )
