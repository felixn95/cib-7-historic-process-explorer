"""Query layer for the process definition list.

``fetch_definitions()`` builds the list of every process definition that appears in the history
or is still deployed -- with instance counts, runtime distribution, deployment state, end
activities, and, where determinable, open incidents and user tasks.

Structured like ``detect.py``: six small queries instead of one monster with a stack of LEFT
JOINs. Each is bounded on its own, observable on its own, and skippable on its own when a table
is missing. No partial failure may abort the whole build; a missing table yields ``None`` or
``0`` depending on which one is truthful, never an exception (see ``_safe_fetch``).

The ``None``-versus-zero rule, concretely for this module: ``open_incidents`` and
``user_task_instances`` are ``None`` when the underlying query failed outright (table absent),
and ``0`` when the query ran but produced no row for that definition -- which is a real result,
not a gap. ``historic_incidents`` follows a different rule: this module does not count
``act_hi_incident`` at all, it only records whether counting would be possible. Hence the field
hangs off ``Feature.HISTORIC_INCIDENTS`` from detection rather than off a query of its own. A
``0`` here means "countable, but not counted in this view", not "no incidents in the history" --
and without a detection result it deliberately stays ``None``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from ..config import Profile
from ..contracts import (
    DefinitionSummary,
    DefinitionVersionRow,
    DetectionResult,
    DurationStats,
    EndActivity,
    Feature,
)
from .connection import Database, DatabaseError

log = logging.getLogger("cib7explorer.db.definitions")

#: Time budget for these queries. They run in a background job with a visible progress
#: indicator, not inside a page request, so deliberately more than the profile's 30 s. Not
#: unbounded either: a query that needs five minutes should be noticed rather than left
#: standing on somebody's database.
BUILD_TIMEOUT_MS = 300_000

#: Row limits per query. Every value here is an aggregate over at most a few hundred groups, so
#: these limits sit comfortably above any realistic installation -- and far below the raw row
#: count of ``act_re_procdef``, which is aggregated in SQL and never fetched.
_LIMIT_INSTANCE_AGGREGATE = 2_000
_LIMIT_DURATION_STATS = 2_000
_LIMIT_DEPLOYMENT_STATUS = 2_000
_LIMIT_END_ACTIVITIES = 5_000
_LIMIT_OPEN_INCIDENTS = 2_000
_LIMIT_USER_TASKS = 2_000

#: Default for ``fetch_versions``. Installations that redeploy on every build accumulate
#: hundreds of versions per key, so this sits generously above that.
_DEFAULT_VERSION_LIMIT = 1_000


# -- SQL constants --------------------------------------------------------------------------
# The test suite collects every constant in this module via vars(definitions) and runs it
# through sqlguard.check().

_SQL_INSTANCE_AGGREGATE = """
    SELECT proc_def_key_ AS key,
           count(*) AS instances,
           count(DISTINCT proc_def_id_) AS versions_used,
           count(*) FILTER (WHERE state_ = 'COMPLETED') AS completed,
           count(*) FILTER (WHERE state_ = 'EXTERNALLY_TERMINATED') AS externally_terminated,
           count(*) FILTER (WHERE state_ = 'INTERNALLY_TERMINATED') AS internally_terminated,
           count(*) FILTER (WHERE state_ = 'ACTIVE') AS active,
           count(*) FILTER (
               WHERE state_ IS NULL
                  OR state_ NOT IN ('COMPLETED', 'EXTERNALLY_TERMINATED', 'INTERNALLY_TERMINATED', 'ACTIVE')
           ) AS state_other,
           count(*) FILTER (WHERE super_process_instance_id_ IS NULL) AS instances_as_root,
           count(*) FILTER (WHERE super_process_instance_id_ IS NOT NULL) AS instances_as_child,
           min(start_time_) AS first_start,
           max(start_time_) AS last_start,
           max(end_time_) AS last_end,
           count(DISTINCT business_key_) AS distinct_business_keys,
           count(*) FILTER (WHERE business_key_ IS NULL) AS instances_without_business_key
      FROM act_hi_procinst
     GROUP BY proc_def_key_
"""

#: Runtime distribution, kept separate from the instance aggregate and restricted to rows with
#: ``duration_ IS NOT NULL``. Percentiles only, no mean -- an average runtime over a skewed
#: distribution is a number that looks informative and is not. The count of unfinished
#: instances (running or terminated, without a ``duration_``) is NOT collected here but derived
#: while assembling, from the difference to the total in ``_SQL_INSTANCE_AGGREGATE``. That saves
#: a third pass over the same table without hiding the number.
_SQL_DURATION_STATS = """
    SELECT proc_def_key_ AS key,
           count(*) AS n,
           min(duration_) AS minimum,
           max(duration_) AS maximum,
           percentile_disc(0.25) WITHIN GROUP (ORDER BY duration_) AS p25,
           percentile_disc(0.5)  WITHIN GROUP (ORDER BY duration_) AS p50,
           percentile_disc(0.75) WITHIN GROUP (ORDER BY duration_) AS p75,
           percentile_disc(0.9)  WITHIN GROUP (ORDER BY duration_) AS p90,
           percentile_disc(0.99) WITHIN GROUP (ORDER BY duration_) AS p99
      FROM act_hi_procinst
     WHERE duration_ IS NOT NULL
     GROUP BY proc_def_key_
"""

#: Deployment state from ``act_re_procdef``. Note the column is called ``key_`` here, not
#: ``proc_def_key_`` as in the history tables. ``array_agg(... ORDER BY version_ DESC)`` yields
#: the names per group in descending version order, so element 1 is the name of the highest
#: version -- possibly NULL, which is left as NULL: the interface then shows the key instead of
#: inventing a name.
_SQL_DEPLOYMENT_STATUS = """
    SELECT key_ AS key,
           count(*) AS deployed_versions,
           max(version_) AS latest_deployed_version,
           count(*) FILTER (WHERE suspension_state_ <> 1) AS suspended_versions,
           (array_agg(name_ ORDER BY version_ DESC))[1] AS latest_name
      FROM act_re_procdef
     GROUP BY key_
"""

#: End activities per definition. Deliberately WITHOUT ``WHERE end_act_id_ IS NOT NULL``: an
#: instance without an end activity (still running, or terminated without a recorded end) forms
#: its own group with ``act_id IS NULL``, which ``classify_end_activity(None, profile)`` maps to
#: ``(False, False)`` -- visible as its own row instead of quietly filtered away.
_SQL_END_ACTIVITIES = """
    SELECT proc_def_key_ AS key,
           end_act_id_ AS act_id,
           count(*) AS n
      FROM act_hi_procinst
     GROUP BY proc_def_key_, end_act_id_
"""

#: ``act_ru_incident`` has no ``proc_def_key_``, only ``proc_def_id_``, hence the join onto
#: ``act_re_procdef``. Open (currently existing) incidents only; the history lives in
#: ``act_hi_incident`` and is not counted here -- see the module docstring.
_SQL_OPEN_INCIDENTS = """
    SELECT d.key_ AS key,
           count(*) AS n
      FROM act_ru_incident i
      JOIN act_re_procdef d ON d.id_ = i.proc_def_id_
     GROUP BY d.key_
"""

_SQL_USER_TASKS = """
    SELECT proc_def_key_ AS key,
           count(*) AS n,
           count(DISTINCT assignee_) AS distinct_assignees
      FROM act_hi_taskinst
     GROUP BY proc_def_key_
"""

#: Counting the validation marker per definition.
#:
#: The expensive lesson behind this query: a marker variable such as ``onlyValidation`` occurs
#: MULTIPLE times within one process instance, and it means two different things depending on
#: where. At instance start it is the input parameter that says how the process was invoked.
#: Later, at some activity inside the process, the same name is often written again by the
#: process itself -- as a result, not as a parameter. Counting "any occurrence is true" therefore
#: reports nearly every instance as a validation run and overstates the number badly.
#:
#: So only the value at instance start counts (``act_inst_id_ = proc_inst_id_``). Restricted
#: that way the value is unambiguous per instance. Instances that carry the marker only
#: internally are reported separately ("not set at start") and are NOT guessed at.
#:
#: Both spellings are merged (``boolean`` and ``string``), because processes disagree about
#: which type to use and filtering on one of them loses most of the hits.
_SQL_VALIDATION_FLAG = """
    WITH marker AS (
        SELECT proc_inst_id_,
               proc_def_key_,
               bool_or(act_inst_id_ = proc_inst_id_) AS has_input,
               bool_and(
                   CASE WHEN var_type_ = 'boolean' AND long_ = 1              THEN true
                        WHEN var_type_ = 'boolean' AND long_ = 0              THEN false
                        WHEN var_type_ = 'string'  AND lower(text_) = 'true'  THEN true
                        WHEN var_type_ = 'string'  AND lower(text_) = 'false' THEN false
                   END
               ) FILTER (WHERE act_inst_id_ = proc_inst_id_) AS input_flag
          FROM act_hi_varinst
         WHERE name_ = %(name)s
           AND proc_def_key_ IS NOT NULL
         GROUP BY 1, 2
    )
    SELECT proc_def_key_,
           count(*) FILTER (WHERE has_input AND input_flag)          AS flag_true,
           count(*) FILTER (WHERE has_input AND input_flag IS false) AS flag_false,
           count(*) FILTER (WHERE has_input AND input_flag IS NULL)  AS flag_undecidable,
           count(*) FILTER (WHERE NOT has_input)                     AS flag_not_at_start
      FROM marker
     GROUP BY 1
"""

#: Name of the marker variable. A parameter rather than a profile field: it is a modelling
#: convention, and callers that need a different name can pass one.
VALIDATION_FLAG_VARIABLE = "onlyValidation"

#: Version breakdown for one definition. Both joins are ``LEFT``, because a ``proc_def_id_``
#: from the history need not still appear in ``act_re_procdef``: history cleanup and deployment
#: cleanup happen independently of each other.
_SQL_VERSIONS = """
    SELECT h.proc_def_id_ AS proc_def_id,
           d.version_ AS version,
           d.version_tag_ AS version_tag,
           dep.deploy_time_ AS deployed_at,
           count(*) AS instances,
           min(h.start_time_) AS first_start,
           max(h.start_time_) AS last_start
      FROM act_hi_procinst h
      LEFT JOIN act_re_procdef d ON d.id_ = h.proc_def_id_
      LEFT JOIN act_re_deployment dep ON dep.id_ = d.deployment_id_
     WHERE h.proc_def_key_ = %(key)s
     GROUP BY h.proc_def_id_, d.version_, d.version_tag_, dep.deploy_time_
     ORDER BY d.version_ DESC NULLS LAST, h.proc_def_id_ DESC
"""


# -- small helper that absorbs partial failures (mirrors detect._safe_fetch) -----------------

def _safe_fetch(
    db: Database, sql: str, params: Any = None, *, limit: int, name: str,
    timeout_ms: int | None = None,
):
    try:
        return db.fetch(sql, params, limit=limit, timeout_ms=timeout_ms, name=name)
    except DatabaseError as exc:
        log.warning("sub-query '%s' failed: %s", name, exc)
        return None


# -- individual steps ------------------------------------------------------------------------

def _fetch_instance_aggregate(
    db: Database, timeout_ms: int | None, progress: Any,
) -> dict[str, dict[str, Any]]:
    r = _safe_fetch(
        db, _SQL_INSTANCE_AGGREGATE, limit=_LIMIT_INSTANCE_AGGREGATE,
        name="def_instance_aggregate", timeout_ms=timeout_ms,
    )
    if r is None:
        if progress:
            progress.note(
                "instance aggregate (act_hi_procinst) unavailable -- per-definition instance "
                "counts stay at 0."
            )
        return {}
    if r.truncated:
        msg = (
            f"instance aggregate over act_hi_procinst truncated at {r.limit} rows -- there are "
            "more definitions with instances than expected, so the list is incomplete."
        )
        log.warning(msg)
        if progress:
            progress.note(msg)
    return {row["key"]: row for row in r.dicts()}


def _fetch_duration_stats(
    db: Database, timeout_ms: int | None, progress: Any,
) -> dict[str, dict[str, Any]]:
    r = _safe_fetch(
        db, _SQL_DURATION_STATS, limit=_LIMIT_DURATION_STATS,
        name="def_duration_stats", timeout_ms=timeout_ms,
    )
    if r is None:
        if progress:
            progress.note("runtime distribution unavailable -- percentiles stay empty.")
        return {}
    if r.truncated:
        msg = f"runtime distribution truncated at {r.limit} rows."
        log.warning(msg)
        if progress:
            progress.note(msg)
    return {row["key"]: row for row in r.dicts()}


def _fetch_deployment_status(
    db: Database, timeout_ms: int | None, progress: Any,
) -> dict[str, dict[str, Any]]:
    r = _safe_fetch(
        db, _SQL_DEPLOYMENT_STATUS, limit=_LIMIT_DEPLOYMENT_STATUS,
        name="def_deployment_status", timeout_ms=timeout_ms,
    )
    if r is None:
        if progress:
            progress.note(
                "deployment state (act_re_procdef) unavailable -- 'deployed' cannot be "
                "confirmed for any definition."
            )
        return {}
    if r.truncated:
        msg = f"deployment state truncated at {r.limit} rows."
        log.warning(msg)
        if progress:
            progress.note(msg)
    return {row["key"]: row for row in r.dicts()}


def _fetch_end_activities(
    db: Database, timeout_ms: int | None, progress: Any,
) -> dict[str, list[dict[str, Any]]]:
    r = _safe_fetch(
        db, _SQL_END_ACTIVITIES, limit=_LIMIT_END_ACTIVITIES,
        name="def_end_activities", timeout_ms=timeout_ms,
    )
    if r is None:
        if progress:
            progress.note("end activities unavailable -- validation-only shares are missing.")
        return {}
    if r.truncated:
        msg = f"end activities truncated at {r.limit} rows."
        log.warning(msg)
        if progress:
            progress.note(msg)
    out: dict[str, list[dict[str, Any]]] = {}
    for row in r.dicts():
        out.setdefault(row["key"], []).append(row)
    return out


def _fetch_open_incidents(
    db: Database, timeout_ms: int | None, progress: Any,
) -> dict[str, int] | None:
    """``None`` means "not determinable at all" (table absent); an empty or partial dict means
    "determined, and definitions without an entry have 0 open incidents"."""
    r = _safe_fetch(
        db, _SQL_OPEN_INCIDENTS, limit=_LIMIT_OPEN_INCIDENTS,
        name="def_open_incidents", timeout_ms=timeout_ms,
    )
    if r is None:
        if progress:
            progress.note("open incidents (act_ru_incident) not determinable.")
        return None
    if r.truncated:
        msg = f"open-incident aggregate truncated at {r.limit} rows."
        log.warning(msg)
        if progress:
            progress.note(msg)
    return {row["key"]: row["n"] for row in r.dicts()}


def _fetch_user_tasks(
    db: Database, timeout_ms: int | None, progress: Any,
) -> dict[str, dict[str, Any]] | None:
    """Like ``_fetch_open_incidents``: ``None`` means not determinable at all."""
    r = _safe_fetch(
        db, _SQL_USER_TASKS, limit=_LIMIT_USER_TASKS,
        name="def_user_tasks", timeout_ms=timeout_ms,
    )
    if r is None:
        if progress:
            progress.note("user tasks (act_hi_taskinst) not determinable.")
        return None
    if r.truncated:
        msg = f"user-task aggregate truncated at {r.limit} rows."
        log.warning(msg)
        if progress:
            progress.note(msg)
    return {row["key"]: row for row in r.dicts()}


# -- pure classification, no database involved -----------------------------------------------

def _fetch_validation_flag(
    db: Database, timeout_ms: int, progress: Any | None,
    variable: str = VALIDATION_FLAG_VARIABLE,
) -> dict[str, dict[str, int]] | None:
    """Count, per definition, how many instances are marked as a validation-only run."""
    if progress is not None:
        progress.step(f"counting validation marker '{variable}'")
    res = _safe_fetch(db, _SQL_VALIDATION_FLAG, {"name": variable}, limit=5000,
                      name="def_validation_flag", timeout_ms=timeout_ms)
    if res is None:
        return None
    if res.truncated and progress is not None:
        progress.note("row limit hit while counting the validation marker -- numbers incomplete.")
    return {r["proc_def_key_"]: r for r in res.dicts()}


def classify_end_activity(act_id: str | None, profile: Profile) -> tuple[bool, bool]:
    """Classify an end activity using the profile's pattern lists.

    Returns ``(validation_only, validation_related)``. End event naming is rarely consistent
    within one installation (``EndEvent_ValidationOnly``, ``end_With_Validation_Only``,
    ``EndEvent_ValidationFailed``, ...), which is why these are pattern lists rather than fixed
    names -- editable per profile, without recomputing the catalogue. A missing ``act_id``
    (running, or terminated without a recorded end) yields ``(False, False)`` rather than a
    guess.
    """
    if not act_id:
        return (False, False)
    validation_only = any(re.search(p, act_id) for p in profile.validation_only_patterns)
    validation_related = any(re.search(p, act_id) for p in profile.validation_result_patterns)
    return (validation_only, validation_related)


# -- assembling -------------------------------------------------------------------------------

def _duration_stats(dur_row: dict[str, Any] | None, total_instances: int) -> DurationStats:
    if dur_row is None:
        return DurationStats(n=0, n_unfinished=total_instances)
    n = int(dur_row["n"])
    return DurationStats(
        n=n,
        n_unfinished=max(total_instances - n, 0),
        p25=dur_row["p25"],
        p50=dur_row["p50"],
        p75=dur_row["p75"],
        p90=dur_row["p90"],
        p99=dur_row["p99"],
        minimum=dur_row["minimum"],
        maximum=dur_row["maximum"],
    )


def _end_activity_tuple(
    rows: list[dict[str, Any]], profile: Profile,
) -> tuple[EndActivity, ...]:
    classified = []
    for row in rows:
        act_id = row["act_id"]
        validation_only, validation_related = classify_end_activity(act_id, profile)
        classified.append(EndActivity(
            act_id=act_id, instances=row["n"],
            validation_only=validation_only, validation_related=validation_related,
        ))
    classified.sort(key=lambda e: -e.instances)
    return tuple(classified)


def _build_summary(
    key: str,
    profile: Profile,
    *,
    inst: dict[str, Any] | None,
    dep: dict[str, Any] | None,
    dur: dict[str, Any] | None,
    end_rows: list[dict[str, Any]],
    open_incidents: int | None,
    task_row: dict[str, Any] | None,
    task_data_available: bool = True,
    validation_row: dict[str, Any] | None = None,
    historic_incidents: int | None,
) -> DefinitionSummary:
    total_instances = int(inst["instances"]) if inst else 0
    return DefinitionSummary(
        key=key,
        name=(dep["latest_name"] if dep else None),
        deployed=dep is not None,
        deployed_versions=(dep["deployed_versions"] if dep else None),
        latest_deployed_version=(dep["latest_deployed_version"] if dep else None),
        suspended_versions=(dep["suspended_versions"] if dep else None),
        versions_used=(inst["versions_used"] if inst else 0),
        instances=total_instances,
        instances_as_root=(inst["instances_as_root"] if inst else 0),
        instances_as_child=(inst["instances_as_child"] if inst else 0),
        completed=(inst["completed"] if inst else 0),
        externally_terminated=(inst["externally_terminated"] if inst else 0),
        internally_terminated=(inst["internally_terminated"] if inst else 0),
        active=(inst["active"] if inst else 0),
        state_other=(inst["state_other"] if inst else 0),
        first_start=(inst["first_start"] if inst else None),
        last_start=(inst["last_start"] if inst else None),
        last_end=(inst["last_end"] if inst else None),
        distinct_business_keys=(inst["distinct_business_keys"] if inst else 0),
        instances_without_business_key=(inst["instances_without_business_key"] if inst else 0),
        duration=_duration_stats(dur, total_instances),
        open_incidents=open_incidents,
        historic_incidents=historic_incidents,
        # A definition without a row in act_hi_taskinst has 0 user tasks -- a real zero.
        # "not recorded" applies only when the query was impossible in the first place.
        user_task_instances=(
            (task_row["n"] if task_row else 0) if task_data_available else None),
        distinct_assignees=(
            (task_row["distinct_assignees"] if task_row else 0) if task_data_available else None),
        end_activities=_end_activity_tuple(end_rows, profile),
        validation_flag_true=(validation_row["flag_true"] if validation_row else None),
        validation_flag_false=(validation_row["flag_false"] if validation_row else None),
        validation_flag_undecidable=(
            validation_row["flag_undecidable"] if validation_row else None),
        validation_flag_not_at_start=(
            validation_row["flag_not_at_start"] if validation_row else None),
    )


def _historic_incidents_value(detection: DetectionResult | None) -> int | None:
    """Decide whether ``historic_incidents`` may carry a number at all.

    A pure function, so the None-versus-zero rule is testable without a database: without a
    detection result, or without ``Feature.HISTORIC_INCIDENTS`` being available, it stays
    ``None`` ("not determinable") and never becomes ``0``.
    """
    if detection is not None and detection.has(Feature.HISTORIC_INCIDENTS):
        return 0
    return None


# -- entry points -----------------------------------------------------------------------------

def fetch_definitions(
    db: Database,
    profile: Profile,
    *,
    detection: DetectionResult | None = None,
    progress: Any = None,
    timeout_ms: int = BUILD_TIMEOUT_MS,
) -> list[DefinitionSummary]:
    """Build the process definition list.

    Includes both definitions with instances and definitions that are deployed but never ran
    (``instances=0, deployed=True``). Whether a definition is still deployed or exists only
    historically is exactly the kind of thing this list is read for, and that question runs in
    both directions.

    Ordering: descending by instance count -- practical relevance rather than alphabetical.
    Definitions without instances therefore fall to the end, where they are sorted by key so
    the result is reproducible.

    No partial failure may let an exception escape -- see the module docstring.
    """
    started = time.perf_counter()

    if progress:
        progress.step("aggregating instances per definition (act_hi_procinst)")
    instances = _fetch_instance_aggregate(db, timeout_ms, progress)

    if progress:
        progress.step("computing runtime distribution (duration_ IS NOT NULL only)")
    durations = _fetch_duration_stats(db, timeout_ms, progress)

    if progress:
        progress.step("reading deployment state (act_re_procdef)")
    deployments = _fetch_deployment_status(db, timeout_ms, progress)

    if progress:
        progress.step("evaluating end activities")
    end_activities = _fetch_end_activities(db, timeout_ms, progress)

    if progress:
        progress.step("counting open incidents (act_ru_incident)")
    open_incidents = _fetch_open_incidents(db, timeout_ms, progress)

    if progress:
        progress.step("counting user tasks (act_hi_taskinst)")
    user_tasks = _fetch_user_tasks(db, timeout_ms, progress)
    validation_flags = _fetch_validation_flag(db, timeout_ms, progress)

    # Historic incidents: this module does not count act_hi_incident (see the module
    # docstring) -- only whether counting would be possible at all, which detection knows.
    historic_incidents_value = _historic_incidents_value(detection)

    keys = sorted(set(instances) | set(deployments))
    summaries = [
        _build_summary(
            key, profile,
            inst=instances.get(key),
            dep=deployments.get(key),
            dur=durations.get(key),
            end_rows=end_activities.get(key, []),
            open_incidents=(None if open_incidents is None else open_incidents.get(key, 0)),
            task_row=(None if user_tasks is None else user_tasks.get(key)),
            task_data_available=(user_tasks is not None),
            validation_row=(None if validation_flags is None else validation_flags.get(key)),
            historic_incidents=historic_incidents_value,
        )
        for key in keys
    ]
    summaries.sort(key=lambda s: (-s.instances, s.key))

    duration_ms = int((time.perf_counter() - started) * 1000)
    if progress:
        progress.step(f"assembled {len(summaries)} definitions ({duration_ms} ms)")
    log.info("fetch_definitions: %d definitions in %d ms", len(summaries), duration_ms)
    return summaries


def fetch_versions(
    db: Database,
    key: str,
    *,
    limit: int = _DEFAULT_VERSION_LIMIT,
    timeout_ms: int | None = None,
) -> list[DefinitionVersionRow]:
    """Version breakdown for one definition, newest version first.

    On demand, not part of what ``fetch_definitions`` returns: installations that redeploy on
    every build accumulate hundreds of versions per key, which would drown the overview list.
    """
    r = _safe_fetch(
        db, _SQL_VERSIONS, {"key": key}, limit=limit, name="def_versions", timeout_ms=timeout_ms,
    )
    if r is None:
        return []
    if r.truncated:
        log.warning(
            "version breakdown for '%s' truncated at %d rows -- there are more versions with "
            "instances than were fetched.", key, r.limit,
        )
    return [
        DefinitionVersionRow(
            key=key,
            proc_def_id=row["proc_def_id"],
            version=row["version"],
            instances=row["instances"],
            first_start=row["first_start"],
            last_start=row["last_start"],
            deployed_at=row["deployed_at"],
            version_tag=row["version_tag"],
        )
        for row in r.dicts()
    ]
