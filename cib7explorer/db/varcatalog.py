"""The variable catalogue: every process variable, without values.

**The iron rule of this module: it never reads a variable value.**

No ``text_`` or ``text2_`` as content, no ``bytes_``, no ``long_``, no ``double_`` in the
result. The point is that this view stays usable on a production system without a data
protection review first, and that its output can be handed to anyone -- to a data protection
officer, to a domain expert -- without values travelling along.

Only these are permitted:

* Names, types, timestamps, counters -- metadata, not content.
* LENGTHS via ``length(text_)`` and ``octet_length(bytes_)``. Both read only the size header of
  the datatype (for ``bytea``, the varlena header), never the value itself, which is what makes
  a size distribution affordable over millions of rows: nothing is decompressed.
* The ONE exception: the Java class name in ``text2_`` when ``var_type_`` is ``serializable``
  (or another object type). The engine stores the fully qualified class name of the
  (de)serialised class there, not the value -- a type name such as ``java.time.LocalDateTime``
  or ``java.util.ArrayList``, never an instance of a business value. Whether that type name
  could be resolved without the application's own code is reported in
  ``SerializationForm.resolvable_without_application``.

Every ``_SQL_*`` constant is checked by the test suite against both ``sqlguard.check()`` and a
dedicated pattern: ``text_``, ``bytes_``, ``long_`` and ``double_`` may appear in a select list
only inside ``length(...)``/``octet_length(...)``, and ``text2_`` only in the serialisation
query. That is what enforces the rule -- a docstring alone would just be a promise.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from ..config import Profile
from ..contracts import (
    CatalogMeta,
    CrossProcessVariable,
    DetectionResult,
    FirstWriteActivity,
    SerializationForm,
    SizeStats,
    VariableCatalog,
    VariableCatalogEntry,
)
from .connection import Database

log = logging.getLogger("cib7explorer.db.varcatalog")

#: Generous time budget per query. The main aggregate alone can take ten seconds on a
#: mid-sized history, and on a larger or less well indexed system the build must not die on the
#: profile's 30 s statement timeout. This runs in a background job with visible progress, not
#: inside a page. Not unbounded either: a query needing five minutes should be noticed.
BUILD_TIMEOUT_MS = 300_000

#: Per entry, only the most frequent first-write activities and serialisation forms are kept.
#: The remainder is not concealed -- it is mentioned in ``CatalogMeta.notes``.
_TOP_N_ACTIVITIES = 5
_TOP_N_SERIALIZATION_FORMS = 5

#: Row limits per query, set explicitly rather than inherited: the profile's default limit is
#: not enough for the version-range query, which groups by three columns and produces far more
#: rows than the others.
_LIMIT_MAIN = 50_000
_LIMIT_FIRST_WRITE = 20_000
_LIMIT_BYTEARRAY_SIZE = 20_000
_LIMIT_SERIALIZATION = 50_000
_LIMIT_DEF_INSTANCES = 5_000
_LIMIT_LATEST_VERSION = 5_000
_LIMIT_VERSION_PAIRS = 400_000
_LIMIT_PROCDEF_VERSIONS = 100_000
_LIMIT_MULTI_SCOPE = 50_000

_STEPS_TOTAL = 8


# -- SQL constants --------------------------------------------------------------------------
# The test suite collects every constant in this module via vars(varcatalog) and checks it
# against sqlguard.check() AND against the no-value-reading rule (see the module docstring).

#: (A) Main aggregate: occurrences, types, period, scope counters and inline sizes per
#: (definition, name). ``length(text_)`` reads the character length, never the content.
_SQL_MAIN = """
    SELECT proc_def_key_                                                        AS def_key,
           name_                                                                AS name,
           count(*)                                                            AS occurrences,
           count(DISTINCT proc_inst_id_)                                       AS instances_with,
           count(DISTINCT proc_def_id_)                                        AS def_id_count,
           count(DISTINCT var_type_)                                           AS type_count,
           string_agg(DISTINCT var_type_, ',')                                 AS types,
           min(create_time_)                                                   AS first_seen,
           max(create_time_)                                                   AS last_seen,
           count(*) FILTER (WHERE act_inst_id_ = proc_inst_id_)                AS at_instance_level,
           count(*) FILTER (WHERE execution_id_ <> proc_inst_id_)              AS below_process_instance,
           count(*) FILTER (WHERE task_id_ IS NOT NULL)                        AS task_local,
           count(*) FILTER (WHERE var_type_ = 'null')                         AS null_typed,
           count(*) FILTER (WHERE bytearray_id_ IS NOT NULL)                   AS with_bytearray,
           count(length(text_))                                                AS inline_n,
           min(length(text_))                                                  AS inline_min,
           percentile_disc(0.5) WITHIN GROUP (ORDER BY length(text_))          AS inline_p50,
           percentile_disc(0.9) WITHIN GROUP (ORDER BY length(text_))          AS inline_p90,
           max(length(text_))                                                  AS inline_max
      FROM act_hi_varinst
     GROUP BY proc_def_key_, name_
"""

#: (B) Where a variable was first written, when that was not instance level: join
#: ``act_inst_id_`` onto the activity that created it. This separates input parameters
#: (instance level) from values produced during the run, and shows what is handed out through a
#: call activity (``act_type_ = 'callActivity'``).
_SQL_FIRST_WRITE = """
    SELECT v.proc_def_key_ AS def_key,
           v.name_         AS name,
           a.act_id_       AS act_id,
           a.act_type_     AS act_type,
           count(*)        AS occurrences
      FROM act_hi_varinst v
      JOIN act_hi_actinst a ON a.id_ = v.act_inst_id_
     WHERE v.act_inst_id_ <> v.proc_inst_id_
     GROUP BY v.proc_def_key_, v.name_, a.act_id_, a.act_type_
"""

#: (C) Sizes of the values stored in ``act_ge_bytearray``. ``octet_length(bytes_)`` reads the
#: varlena header, not the content -- cheap enough for millions of rows.
_SQL_BYTEARRAY_SIZE = """
    SELECT v.proc_def_key_                                                     AS def_key,
           v.name_                                                             AS name,
           count(*)                                                            AS n,
           min(octet_length(b.bytes_))                                         AS minimum,
           percentile_disc(0.5) WITHIN GROUP (ORDER BY octet_length(b.bytes_)) AS p50,
           percentile_disc(0.9) WITHIN GROUP (ORDER BY octet_length(b.bytes_)) AS p90,
           max(octet_length(b.bytes_))                                         AS maximum
      FROM act_hi_varinst v
      JOIN act_ge_bytearray b ON b.id_ = v.bytearray_id_
     GROUP BY v.proc_def_key_, v.name_
"""

#: (D) Serialisation forms. Here ``text2_`` is explicitly NOT a value but the fully qualified
#: class name (for ``var_type_ = 'serializable'``) or the type marker of the serialised object
#: (json/xml/bytes) -- the one exception to the iron rule, see the module docstring.
#: ``var_type_ = 'string'`` is deliberately excluded: for those, ``text2_`` holds only the
#: ``!emptyString!`` marker, which says nothing.
_SQL_SERIALIZATION = """
    SELECT proc_def_key_ AS def_key,
           name_         AS name,
           var_type_     AS var_type,
           text2_        AS java_class,
           count(*)      AS occurrences
      FROM act_hi_varinst
     WHERE var_type_ IN ('serializable', 'json', 'xml', 'bytes')
     GROUP BY proc_def_key_, name_, var_type_, text2_
"""

#: (E1) Which ``proc_def_id_`` values actually occur per (definition, name) -- the basis for
#: the version range, aggregated in Python (see ``_fetch_version_ranges`` for why).
_SQL_VERSION_PAIRS = """
    SELECT proc_def_key_ AS def_key,
           name_         AS name,
           proc_def_id_  AS proc_def_id
      FROM act_hi_varinst
     GROUP BY proc_def_key_, name_, proc_def_id_
"""

#: (E2) Mapping ``proc_def_id_`` -> version. Even in installations that redeploy constantly
#: this table is small enough to fetch once in full, rather than joining it against millions of
#: variable rows.
_SQL_PROCDEF_VERSIONS = "SELECT id_ AS proc_def_id, version_ AS version FROM act_re_procdef"

#: (F) Several variable instances of the same name within one process instance. This is NOT
#: overwriting (``act_hi_detail`` is empty at history level AUDIT) but several scopes --
#: multi-instance loops or subprocesses.
_SQL_MULTI_SCOPE = """
    SELECT def_key, name, count(*) AS instances_multi_scope
      FROM (
            SELECT proc_def_key_ AS def_key, name_ AS name, proc_inst_id_,
                   count(*) AS occurrences_in_instance
              FROM act_hi_varinst
             GROUP BY proc_def_key_, name_, proc_inst_id_
           ) per_instance
     WHERE occurrences_in_instance > 1
     GROUP BY def_key, name
"""

#: The denominator. A variable present in 3 % of instances is a completely different thing
#: from one present in 99 %, so the count of instances per definition is not optional. Only
#: queried here when ``build_catalog`` is not given it by the caller.
_SQL_DEF_INSTANCES = "SELECT proc_def_key_ AS def_key, count(*) AS n FROM act_hi_procinst GROUP BY proc_def_key_"

#: Highest version per key WITH at least one instance -- only queried when ``build_catalog``
#: is not given it by the caller.
_SQL_LATEST_USED_VERSION = """
    SELECT p.proc_def_key_ AS def_key, max(d.version_) AS version
      FROM act_hi_procinst p
      JOIN act_re_procdef d ON d.id_ = p.proc_def_id_
     GROUP BY p.proc_def_key_
"""


# -- small helper structures ------------------------------------------------------------------

_Key = tuple[str, str]  # (def_key, name)


def _split_types(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(sorted(t for t in raw.split(",") if t))


# -- the individual steps -----------------------------------------------------------------------------

def _fetch_def_instances(db: Database, timeout_ms: int) -> dict[str, int]:
    r = db.fetch(_SQL_DEF_INSTANCES, limit=_LIMIT_DEF_INSTANCES, timeout_ms=timeout_ms, name="var_def_instances")
    return {row["def_key"]: row["n"] for row in r.dicts()}


def _fetch_latest_used_version(db: Database, timeout_ms: int) -> dict[str, int]:
    r = db.fetch(_SQL_LATEST_USED_VERSION, limit=_LIMIT_LATEST_VERSION, timeout_ms=timeout_ms,
                 name="var_latest_used_version")
    return {row["def_key"]: row["version"] for row in r.dicts() if row["version"] is not None}


def _fetch_first_write_activities(
    db: Database, timeout_ms: int,
) -> tuple[dict[_Key, list[FirstWriteActivity]], dict[_Key, int], bool]:
    """Per (def_key, name): the top ``_TOP_N_ACTIVITIES`` first-write activities by frequency,
    plus the total number written at a call activity."""
    r = db.fetch(_SQL_FIRST_WRITE, limit=_LIMIT_FIRST_WRITE, timeout_ms=timeout_ms, name="var_first_write")
    raw: dict[_Key, list[FirstWriteActivity]] = defaultdict(list)
    for row in r.dicts():
        key = (row["def_key"], row["name"])
        raw[key].append(FirstWriteActivity(
            act_id=row["act_id"], act_type=row["act_type"], occurrences=row["occurrences"],
        ))

    top: dict[_Key, list[FirstWriteActivity]] = {}
    from_call_activity: dict[_Key, int] = defaultdict(int)
    for key, activities in raw.items():
        activities.sort(key=lambda a: a.occurrences, reverse=True)
        top[key] = activities[:_TOP_N_ACTIVITIES]
        from_call_activity[key] = sum(a.occurrences for a in activities if a.act_type == "callActivity")
    return top, from_call_activity, r.truncated


def _fetch_bytearray_sizes(db: Database, timeout_ms: int) -> tuple[dict[_Key, SizeStats], bool]:
    r = db.fetch(_SQL_BYTEARRAY_SIZE, limit=_LIMIT_BYTEARRAY_SIZE, timeout_ms=timeout_ms,
                 name="var_bytearray_size")
    out: dict[_Key, SizeStats] = {}
    for row in r.dicts():
        out[(row["def_key"], row["name"])] = SizeStats(
            n=row["n"], minimum=row["minimum"], p50=row["p50"], p90=row["p90"], maximum=row["maximum"],
        )
    return out, r.truncated


def _fetch_serialization_forms(db: Database, timeout_ms: int) -> tuple[dict[_Key, list[SerializationForm]], bool]:
    r = db.fetch(_SQL_SERIALIZATION, limit=_LIMIT_SERIALIZATION, timeout_ms=timeout_ms,
                 name="var_serialization")
    raw: dict[_Key, list[SerializationForm]] = defaultdict(list)
    for row in r.dicts():
        key = (row["def_key"], row["name"])
        raw[key].append(SerializationForm(
            var_type=row["var_type"], java_class=row["java_class"], occurrences=row["occurrences"],
        ))
    top: dict[_Key, list[SerializationForm]] = {}
    for key, forms in raw.items():
        forms.sort(key=lambda f: f.occurrences, reverse=True)
        top[key] = forms[:_TOP_N_SERIALIZATION_FORMS]
    return top, r.truncated


def _fetch_version_ranges(db: Database, timeout_ms: int) -> tuple[dict[_Key, tuple[int | None, int | None]], bool]:
    """Version range per (def_key, name).

    Measured on a multi-million-row history: joining the variable table directly onto
    ``act_re_procdef`` and aggregating in SQL took more than twice as long as the approach used
    here. Instead, two cheap queries run: which ``proc_def_id_`` values occur per pair
    (grouping by three columns instead of two), and separately the small mapping
    ``proc_def_id_`` -> version. The min/max per pair is then folded in Python. PostgreSQL
    avoids the expensive join between a huge and a small table entirely, and the total is
    roughly half the time.
    """
    pairs = db.fetch(_SQL_VERSION_PAIRS, limit=_LIMIT_VERSION_PAIRS, timeout_ms=timeout_ms,
                      name="var_version_pairs")
    versions = db.fetch(_SQL_PROCDEF_VERSIONS, limit=_LIMIT_PROCDEF_VERSIONS, timeout_ms=timeout_ms,
                         name="var_procdef_versions")
    id_to_version = {row["proc_def_id"]: row["version"] for row in versions.dicts()}

    ranges: dict[_Key, tuple[int | None, int | None]] = {}
    mins: dict[_Key, int] = {}
    maxs: dict[_Key, int] = {}
    for row in pairs.dicts():
        version = id_to_version.get(row["proc_def_id"])
        if version is None:
            continue
        key = (row["def_key"], row["name"])
        if key not in mins or version < mins[key]:
            mins[key] = version
        if key not in maxs or version > maxs[key]:
            maxs[key] = version
    for key in set(mins) | set(maxs):
        ranges[key] = (mins.get(key), maxs.get(key))
    return ranges, (pairs.truncated or versions.truncated)


def _fetch_multi_scope(db: Database, timeout_ms: int) -> tuple[dict[_Key, int], bool]:
    r = db.fetch(_SQL_MULTI_SCOPE, limit=_LIMIT_MULTI_SCOPE, timeout_ms=timeout_ms, name="var_multi_scope")
    return {(row["def_key"], row["name"]): row["instances_multi_scope"] for row in r.dicts()}, r.truncated


# -- assembling the result ------------------------------------------------------------------------------

def build_catalog(
    db: Database,
    profile: Profile,
    *,
    def_instances: dict[str, int] | None = None,
    latest_used_version: dict[str, int] | None = None,
    detection: DetectionResult | None = None,
    progress: Any = None,
    timeout_ms: int = BUILD_TIMEOUT_MS,
) -> VariableCatalog:
    """Build the variable catalogue for every process definition.

    ``def_instances`` is the denominator per definition (instance counts from
    ``cib7explorer.db.definitions``), ``latest_used_version`` the highest version per key WITH
    instances. When they are missing they are queried here: a share without its denominator is
    not a number worth showing, so the denominator does not become optional just because the
    caller skipped a precomputation.

    Reads ``text_``, ``bytes_``, ``long_`` and ``double_`` only as lengths, and ``text2_`` only
    as a class name -- see the module docstring.
    """
    started = time.perf_counter()
    notes: list[str] = [
        "act_hi_detail is empty at history level AUDIT: whether a variable was set once or "
        "overwritten many times cannot be answered from this data. 'instances_multi_scope' "
        "shows something else -- process instances in which the same name occurs more than "
        "once (multi-instance loops, subprocesses), not the overwriting of a single value.",
        f"Per entry, only the {_TOP_N_ACTIVITIES} most frequent first-write activities and the "
        f"{_TOP_N_SERIALIZATION_FORMS} most frequent serialisation forms are kept; rarer ones "
        "exist in the underlying database but are not listed individually here.",
    ]

    def _step(msg: str) -> None:
        if progress is not None:
            progress.step(msg)

    def _note(msg: str) -> None:
        if progress is not None:
            progress.note(msg)

    if def_instances is None:
        _step("determining instance count per definition (the denominator)")
        def_instances = _fetch_def_instances(db, timeout_ms)
    if latest_used_version is None:
        _step("determining highest used version per definition")
        latest_used_version = _fetch_latest_used_version(db, timeout_ms)

    _step("main aggregate: occurrences, types, period, scope and inline sizes")
    main = db.fetch(_SQL_MAIN, limit=_LIMIT_MAIN, timeout_ms=timeout_ms, name="var_main")
    if main.truncated:
        notes.append(
            f"The main aggregate was truncated at {main.limit} rows -- the catalogue "
            "is incomplete for this data."
        )
        log.warning("main aggregate of the variable catalogue truncated at %d rows", main.limit)

    _step("determining first write per activity (input parameter vs. produced)")
    first_write, from_call_activity, fw_truncated = _fetch_first_write_activities(db, timeout_ms)
    if fw_truncated:
        notes.append("The first-write query was truncated.")

    _step("measuring sizes of values stored in act_ge_bytearray")
    bytearray_sizes, ba_truncated = _fetch_bytearray_sizes(db, timeout_ms)
    if ba_truncated:
        notes.append("The bytearray size query was truncated.")

    _step("evaluating serialisation forms (Java object, JSON, XML)")
    serialization, ser_truncated = _fetch_serialization_forms(db, timeout_ms)
    if ser_truncated:
        notes.append("The serialisation form query was truncated.")

    _step("determining version range per variable")
    version_ranges, vr_truncated = _fetch_version_ranges(db, timeout_ms)
    if vr_truncated:
        notes.append("The version range query was truncated.")

    _step("counting repeated occurrences per instance (multi-instance/subprocess)")
    multi_scope, ms_truncated = _fetch_multi_scope(db, timeout_ms)
    if ms_truncated:
        notes.append("The repeated-occurrence query was truncated.")

    entries: list[VariableCatalogEntry] = []
    for row in main.dicts():
        key = (row["def_key"], row["name"])
        types = _split_types(row["types"])
        below = row["below_process_instance"] or 0
        occurrences = row["occurrences"] or 0
        def_n = def_instances.get(row["def_key"], 0)
        vmin, vmax = version_ranges.get(key, (None, None))
        latest = latest_used_version.get(row["def_key"])

        entries.append(VariableCatalogEntry(
            def_key=row["def_key"],
            name=row["name"],
            types=types,
            type_switch=(row["type_count"] or 0) > 1,
            occurrences=occurrences,
            instances_with=row["instances_with"] or 0,
            def_instances=def_n,
            null_typed=row["null_typed"] or 0,
            instances_multi_scope=multi_scope.get(key, 0),
            first_write_at_instance_level=row["at_instance_level"] or 0,
            first_write_activities=tuple(first_write.get(key, ())),
            from_call_activity=from_call_activity.get(key, 0),
            scope_process_instance=occurrences - below,
            scope_below_process_instance=below,
            scope_task_local=row["task_local"] or 0,
            serialization=tuple(serialization.get(key, ())),
            inline_size=SizeStats(
                n=row["inline_n"] or 0, minimum=row["inline_min"], p50=row["inline_p50"],
                p90=row["inline_p90"], maximum=row["inline_max"],
            ),
            bytearray_size=bytearray_sizes.get(key, SizeStats()),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            version_min=vmin,
            version_max=vmax,
            in_latest_used_version=(vmax == latest) if (vmax is not None and latest is not None) else None,
        ))

    entries.sort(key=lambda e: (e.instances_with, e.occurrences), reverse=True)

    cross_process = build_cross_process(entries)

    duration_ms = int((time.perf_counter() - started) * 1000)
    meta = CatalogMeta(
        built_at=datetime.now(timezone.utc),
        duration_ms=duration_ms,
        profile_name=profile.name,
        installation_id=detection.installation_id if detection else None,
        history_level=detection.history_level.label if detection and detection.history_level else None,
        rows=len(entries),
        notes=tuple(notes),
    )
    _note(f"catalogue complete: {len(entries)} entries in {duration_ms} ms")
    log.info("variable catalogue for profile '%s' complete: %d entries in %d ms",
             profile.name, len(entries), duration_ms)

    return VariableCatalog(entries=tuple(entries), cross_process=tuple(cross_process), meta=meta)


def build_cross_process(entries: Iterable[VariableCatalogEntry]) -> list[CrossProcessVariable]:
    """Cross-process view: names occurring in more than one process definition -- candidates
    for a shared object reference.

    A pure function without database access, so it is testable independently of
    ``build_catalog``. It never claims that two identically named variables mean the same
    thing; it reports only that the name recurs, and whether it carries different types while
    doing so (``type_conflict``).
    """
    grouped: dict[str, list[VariableCatalogEntry]] = defaultdict(list)
    for entry in entries:
        grouped[entry.name].append(entry)

    result: list[CrossProcessVariable] = []
    for name, group in grouped.items():
        def_keys = sorted({e.def_key for e in group})
        if len(def_keys) < 2:
            continue
        types = sorted({t for e in group for t in e.types})
        result.append(CrossProcessVariable(
            name=name,
            def_count=len(def_keys),
            definitions=tuple(def_keys),
            types=tuple(types),
            type_conflict=len(types) > 1,
            occurrences=sum(e.occurrences for e in group),
            instances_with=sum(e.instances_with for e in group),
        ))

    result.sort(key=lambda c: (c.def_count, c.occurrences), reverse=True)
    return result


# -- CSV export -----------------------------------------------------------------------------

_CSV_HEADER = (
    "Process definition", "Variable name", "Types", "Type switch",
    "Occurrences", "Instances with variable", "Instances of definition", "Share of instances (%)",
    "Null-typed (no value)", "Share with value (%)",
    "Multiple scopes per instance",
    "First write at instance level", "Most frequent first-write activities",
    "Handed out via call activity",
    "Scope process instance", "Scope below process instance", "Scope task-local",
    "Serialisation forms", "Resolvable without application",
    "Inline size count", "Inline size min", "Inline size p50", "Inline size p90", "Inline size max",
    "Bytearray size count", "Bytearray size min", "Bytearray size p50", "Bytearray size p90",
    "Bytearray size max",
    "First seen", "Last seen",
    "Version min", "Version max", "In latest used version",
)


def _fmt_percent(value: float | None) -> str:
    return "" if value is None else f"{value * 100:.1f}"


def _fmt_iso(value: datetime | None) -> str:
    return "" if value is None else value.isoformat()


def _fmt_bool(value: bool | None) -> str:
    if value is None:
        return ""
    return "yes" if value else "no"


def _fmt_activities(activities: tuple[FirstWriteActivity, ...]) -> str:
    return " | ".join(f"{a.act_id} ({a.act_type or '?'}: {a.occurrences})" for a in activities)


def _fmt_serialization(forms: tuple[SerializationForm, ...]) -> str:
    return " | ".join(
        f"{f.var_type}:{f.java_class or '?'} ({f.occurrences})" for f in forms
    )


def to_csv(catalog: VariableCatalog, *, delimiter: str = ";", bom: bool = True) -> str:
    """Export for people who do not have database access.

    Semicolon delimiter and a BOM by default so that spreadsheet software opens the file
    directly in locales where the comma is the decimal separator. Contains no variable value
    anywhere -- only fields from ``VariableCatalogEntry``, which cannot carry values in the
    first place. Shares as percentages with one decimal, timestamps as ISO 8601.
    """
    lines: list[str] = [delimiter.join(_CSV_HEADER)]

    for e in catalog.entries:
        row = (
            e.def_key,
            e.name,
            "|".join(e.types),
            _fmt_bool(e.type_switch),
            str(e.occurrences),
            str(e.instances_with),
            str(e.def_instances),
            _fmt_percent(e.share_of_instances),
            str(e.null_typed),
            _fmt_percent(e.share_with_value),
            str(e.instances_multi_scope),
            str(e.first_write_at_instance_level),
            _fmt_activities(e.first_write_activities),
            str(e.from_call_activity),
            str(e.scope_process_instance),
            str(e.scope_below_process_instance),
            str(e.scope_task_local),
            _fmt_serialization(e.serialization),
            _fmt_bool(e.resolvability),
            str(e.inline_size.n), _num(e.inline_size.minimum), _num(e.inline_size.p50),
            _num(e.inline_size.p90), _num(e.inline_size.maximum),
            str(e.bytearray_size.n), _num(e.bytearray_size.minimum), _num(e.bytearray_size.p50),
            _num(e.bytearray_size.p90), _num(e.bytearray_size.maximum),
            _fmt_iso(e.first_seen), _fmt_iso(e.last_seen),
            _num(e.version_min), _num(e.version_max), _fmt_bool(e.in_latest_used_version),
        )
        lines.append(delimiter.join(_csv_escape(field, delimiter) for field in row))

    text = "\r\n".join(lines) + "\r\n"
    return ("﻿" + text) if bom else text


def _num(value: int | None) -> str:
    return "" if value is None else str(value)


def _csv_escape(field: str, delimiter: str) -> str:
    if any(c in field for c in (delimiter, '"', "\n", "\r")):
        return '"' + field.replace('"', '""') + '"'
    return field
