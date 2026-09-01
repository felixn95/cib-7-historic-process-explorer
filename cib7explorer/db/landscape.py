"""The process landscape in numbers.

Everything here is **plain counting** -- frequencies, distributions, time series. No scoring, no
thresholds, no anomaly flags. Runtimes and sizes appear as distributions with median and
quartiles, never as an average.

**Division of labour between SQL and Python.** The database does the cheap grouping (monthly
aggregate, call edges). The case-level analysis -- co-occurrence, transitions, sequences -- is
computed in Python from a handful of large but narrow result sets. On a multi-million-row history
that comes to roughly one second in total. The same analysis expressed in SQL would be an
unreadable construction of window functions and self-joins that nobody can reason about while it
is running on somebody's production database.

**About the transitions:** they look like a process model and are not one. They are reported as a
frequency count of observed orderings, rare ones are not filtered away, and the view's threshold
defaults to showing everything. Filtering the tail is exactly how an interesting exception
becomes invisible.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Iterable

from ..config import Profile
from ..contracts import (
    ActorStats,
    CallEdge,
    CoOccurrence,
    DisruptionStats,
    Distribution,
    Feature,
    Landscape,
    LandscapeMeta,
    MonthCount,
    SequencePattern,
    Transition,
)
from .connection import Database, DatabaseError

log = logging.getLogger("cib7explorer.db.landscape")

#: Deliberately more than the 30 s of a page request: this runs in a background job with visible
#: progress. Not unbounded either -- a query needing five minutes should be noticed.
BUILD_TIMEOUT_MS = 300_000

#: Upper bounds of the source queries. When one is hit, a note says so -- the number does not
#: pretend to be complete.
MAX_ROOT_ROWS = 400_000
MAX_INSTANCE_ROWS = 600_000

#: How many sequences, transitions and co-occurrences the precomputation keeps. The totals are
#: retained, so what is missing from the list stays visible.
TOP_SEQUENCES = 400
TOP_TRANSITIONS = 2000
TOP_COOCCURRENCE = 2000

#: Buckets for counting gaps across the whole history: (key, label, upper bound in seconds).
#: The key is plain ASCII because it travels as a URL parameter into the drill-down.
GAP_BUCKETS: tuple[tuple[str, str, float], ...] = (
    ("min1", "up to 1 min", 60),
    ("h1", "up to 1 h", 3600),
    ("d1", "up to 1 day", 86400),
    ("d7", "up to 7 days", 7 * 86400),
    ("d30", "up to 30 days", 30 * 86400),
    ("more", "over 30 days", float("inf")),
)

_SQL_ROOTS = """
    SELECT business_key_, proc_def_key_, start_time_, end_time_, state_
      FROM act_hi_procinst
     WHERE super_process_instance_id_ IS NULL
       AND business_key_ IS NOT NULL
     ORDER BY business_key_, start_time_, proc_inst_id_
"""

_SQL_KEY_INSTANCES = """
    SELECT business_key_, proc_def_key_, start_time_, end_time_
      FROM act_hi_procinst
     WHERE business_key_ IS NOT NULL
"""

_SQL_PARENTS = "SELECT proc_inst_id_, super_process_instance_id_ FROM act_hi_procinst"

_SQL_MONTHLY = """
    SELECT proc_def_key_, date_trunc('month', start_time_) AS month, count(*) AS n,
           count(DISTINCT proc_def_id_) AS versions
      FROM act_hi_procinst
     GROUP BY 1, 2
     ORDER BY 1, 2
"""

_SQL_CALL_EDGES = """
    SELECT p.proc_def_key_ AS parent_def, c.proc_def_key_ AS child_def, count(*) AS n,
           count(DISTINCT p.proc_inst_id_) AS parent_instances
      FROM act_hi_procinst c
      JOIN act_hi_procinst p ON p.proc_inst_id_ = c.super_process_instance_id_
     GROUP BY 1, 2
     ORDER BY 3 DESC
"""

_SQL_ROLES = """
    SELECT proc_def_key_,
           count(*) AS instance_count,
           count(*) FILTER (WHERE super_process_instance_id_ IS NULL) AS as_root,
           count(*) FILTER (WHERE super_process_instance_id_ IS NOT NULL) AS as_child,
           count(*) FILTER (WHERE state_ = 'EXTERNALLY_TERMINATED') AS externally_terminated,
           count(*) FILTER (WHERE state_ = 'INTERNALLY_TERMINATED') AS internally_terminated
      FROM act_hi_procinst
     GROUP BY 1
"""

_SQL_ASSIGNEES = """
    SELECT coalesce(assignee_, '') AS assignee, count(*) AS n,
           count(DISTINCT proc_def_key_) AS definitions
      FROM act_hi_taskinst
     GROUP BY 1
     ORDER BY 2 DESC
"""

_SQL_DEFS_WITH_TASKS = """
    SELECT proc_def_key_, count(*) AS tasks
      FROM act_hi_taskinst
     WHERE proc_def_key_ IS NOT NULL
     GROUP BY 1
"""

_SQL_OPEN_INCIDENTS_BY_DEF = """
    SELECT d.key_ AS def_key, count(*) AS n
      FROM act_ru_incident i
      JOIN act_re_procdef d ON d.id_ = i.proc_def_id_
     GROUP BY 1
"""

_SQL_WINDOW = "SELECT min(start_time_) AS window_from, max(coalesce(end_time_, start_time_)) AS window_to FROM act_hi_procinst"

_SQL_VALIDATION_TOTAL = """
    WITH marker AS (
        SELECT proc_inst_id_,
               bool_and(
                   CASE WHEN var_type_ = 'boolean' AND long_ = 1              THEN true
                        WHEN var_type_ = 'boolean' AND long_ = 0              THEN false
                        WHEN var_type_ = 'string'  AND lower(text_) = 'true'  THEN true
                        WHEN var_type_ = 'string'  AND lower(text_) = 'false' THEN false
                   END) AS flag
          FROM act_hi_varinst
         WHERE name_ = %(name)s
           AND act_inst_id_ = proc_inst_id_
         GROUP BY 1
    )
    SELECT count(*) FILTER (WHERE flag) AS validation_only FROM marker
"""


def _safe(db: Database, sql: str, params: Any = None, *, limit: int, name: str,
          timeout_ms: int = BUILD_TIMEOUT_MS):
    try:
        return db.fetch(sql, params, limit=limit, name=name, timeout_ms=timeout_ms)
    except DatabaseError as exc:
        log.info("sub-query '%s' failed: %s", name, exc)
        return None


def _sequence_of(rows: list[tuple[str, datetime | None, datetime | None]]) -> tuple[str, ...]:
    return tuple(r[0] for r in rows)


def build_landscape(db: Database, profile: Profile, *, detection: Any = None,
                    progress: Any = None, timeout_ms: int = BUILD_TIMEOUT_MS) -> Landscape:
    """Build the whole landscape. Meant for the background job, not for a page request."""
    started = time.perf_counter()
    notes: list[str] = []

    def step(msg: str) -> None:
        if progress is not None:
            progress.step(msg)

    def note(msg: str) -> None:
        notes.append(msg)
        if progress is not None:
            progress.note(msg)

    # -- window and basics ---------------------------------------------------------------
    step("time window and basic figures")
    window_start = window_end = None
    wres = _safe(db, _SQL_WINDOW, limit=2, name="landscape_window", timeout_ms=timeout_ms)
    if wres is not None and wres.rows:
        window_start, window_end = wres.rows[0]

    step("monthly series per process definition")
    monthly: list[MonthCount] = []
    mres = _safe(db, _SQL_MONTHLY, limit=50_000, name="landscape_monthly", timeout_ms=timeout_ms)
    if mres is not None:
        monthly = [MonthCount(def_key=r["proc_def_key_"] or "(no key)", month=r["month"],
                              instances=r["n"], versions=r["versions"])
                   for r in mres.dicts()]

    step("call graph between process definitions")
    call_edges: list[CallEdge] = []
    cres = _safe(db, _SQL_CALL_EDGES, limit=20_000, name="landscape_call_graph",
                 timeout_ms=timeout_ms)
    if cres is not None:
        call_edges = [CallEdge(parent_def=r["parent_def"] or "(no key)",
                              child_def=r["child_def"] or "(no key)",
                              calls=r["n"], parent_instances=r["parent_instances"])
                      for r in cres.dicts()]
    call_pairs = {(e.parent_def, e.child_def) for e in call_edges}

    step("roles of the definitions (root, subprocess, both)")
    only_root: list[str] = []
    only_child: list[str] = []
    both: list[str] = []
    disruption_rows: list[tuple[str, int, int, int, int | None]] = []
    open_incidents: dict[str, int] = {}
    ires = _safe(db, _SQL_OPEN_INCIDENTS_BY_DEF, limit=10_000, name="landscape_incidents",
                 timeout_ms=timeout_ms)
    if ires is not None:
        open_incidents = {r["def_key"]: r["n"] for r in ires.dicts()}
    rres = _safe(db, _SQL_ROLES, limit=20_000, name="landscape_roles", timeout_ms=timeout_ms)
    if rres is not None:
        for r in rres.dicts():
            key = r["proc_def_key_"] or "(no key)"
            if r["as_root"] and r["as_child"]:
                both.append(key)
            elif r["as_child"]:
                only_child.append(key)
            else:
                only_root.append(key)
            disruption_rows.append((key, r["instance_count"], r["externally_terminated"],
                                    r["internally_terminated"], open_incidents.get(key)))
    disruption_rows.sort(key=lambda t: -(t[2] + t[3]))

    step("call depth of the instances")
    depth_distribution: list[tuple[int, int]] = []
    pres = _safe(db, _SQL_PARENTS, limit=MAX_INSTANCE_ROWS, name="landscape_parents",
                 timeout_ms=timeout_ms)
    if pres is not None:
        if pres.truncated:
            note(f"More than {MAX_INSTANCE_ROWS:,} instances -- the depth distribution is "
                 "incomplete.")
        parent_of = {r[0]: r[1] for r in pres.rows}
        cache: dict[str, int] = {}

        def depth(pid: str) -> int:
            """Depth = number of ancestors within the closure.

            Memoised, so that hundreds of thousands of instances do not each walk their own
            chain to the root; the chain is filled in on the way back down.
            """
            chain: list[str] = []
            cur = pid
            base_path = 0
            while True:
                if cur in cache:
                    base_path = cache[cur]
                    break
                parents = parent_of.get(cur)
                if parents is None or parents not in parent_of or len(chain) > 40:
                    base_path = 0
                    break
                chain.append(cur)
                cur = parents
            for distance, node in enumerate(reversed(chain), start=1):
                cache[node] = base_path + distance
            return cache.get(pid, base_path)

        counter: Counter[int] = Counter(depth(pid) for pid in parent_of)
        depth_distribution = sorted(counter.items())

    # -- case level ----------------------------------------------------------------------
    step("root instances per business key (the basis of the case level)")
    roots = _safe(db, _SQL_ROOTS, limit=MAX_ROOT_ROWS, name="landscape_roots",
                  timeout_ms=timeout_ms)
    per_key: dict[str, list[tuple[str, datetime | None, datetime | None]]] = defaultdict(list)
    if roots is not None:
        if roots.truncated:
            note(f"More than {MAX_ROOT_ROWS:,} root instances -- transitions, sequences and "
                 "gaps are incomplete.")
        for bk, def_key, start, end, _state in roots.rows:
            per_key[bk].append((def_key or "(no key)", start, end))

    step("transitions, sequences, entry and exit points")
    transitions: Counter[tuple[str, str]] = Counter()
    transition_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    transition_gaps: dict[tuple[str, str], list[float]] = defaultdict(list)
    sequences: Counter[tuple[str, ...]] = Counter()
    sequence_keys: dict[tuple[str, ...], list[str]] = defaultdict(list)
    entries: Counter[str] = Counter()
    exits: Counter[str] = Counter()
    gap_values: list[float] = []
    gap_bucket_counts: Counter[str] = Counter()
    overlap_pairs = 0
    gap_pairs = 0

    for bk, rows in per_key.items():
        seq = _sequence_of(rows)
        sequences[seq] += 1
        if len(sequence_keys[seq]) < 5:
            sequence_keys[seq].append(bk)
        entries[seq[0]] += 1
        exits[seq[-1]] += 1
        hwm: datetime | None = None
        for (def_a, start_a, end_a), (def_b, start_b, _end_b) in zip(rows, rows[1:]):
            transitions[(def_a, def_b)] += 1
            transition_keys[(def_a, def_b)].add(bk)
            # Gap against the running high-water mark, as in the case view
            hwm = end_a if hwm is None or (end_a and end_a > hwm) else hwm
            if hwm is not None and start_b is not None:
                delta = (start_b - hwm).total_seconds()
                if delta > 0:
                    gap_pairs += 1
                    gap_values.append(delta)
                    transition_gaps[(def_a, def_b)].append(delta)
                    for key, _label, limit in GAP_BUCKETS:
                        if delta <= limit:
                            gap_bucket_counts[key] += 1
                            break
                else:
                    overlap_pairs += 1

    step("co-occurrence at case level")
    all_inst = _safe(db, _SQL_KEY_INSTANCES, limit=MAX_INSTANCE_ROWS,
                     name="landscape_key_instances", timeout_ms=timeout_ms)
    defs_per_key: dict[str, set[str]] = defaultdict(set)
    inst_per_key: Counter[str] = Counter()
    span_per_key: dict[str, list[datetime]] = defaultdict(list)
    if all_inst is not None:
        if all_inst.truncated:
            note(f"More than {MAX_INSTANCE_ROWS:,} instances with a business key -- "
                 "co-occurrence and size distributions are incomplete.")
        for bk, def_key, start, end in all_inst.rows:
            defs_per_key[bk].add(def_key or "(no key)")
            inst_per_key[bk] += 1
            if start:
                span_per_key[bk].append(start)
            if end:
                span_per_key[bk].append(end)

    co_counter: Counter[tuple[str, str]] = Counter()
    for bk, defs in defs_per_key.items():
        for a, b in combinations(sorted(defs), 2):
            co_counter[(a, b)] += 1

    co_occurrence = [
        CoOccurrence(def_a=a, def_b=b, keys=n,
                     also_calls=((a, b) in call_pairs or (b, a) in call_pairs))
        for (a, b), n in co_counter.most_common(TOP_COOCCURRENCE)
    ]

    step("distributions: size, duration, gaps")
    instances_per_key = Distribution.from_values([float(v) for v in inst_per_key.values()])
    definitions_per_key = Distribution.from_values([float(len(v)) for v in defs_per_key.values()])
    span_values = [
        (max(ts) - min(ts)).total_seconds() * 1000.0
        for ts in span_per_key.values() if len(ts) >= 2
    ]
    span_dist = Distribution.from_values(span_values)
    gaps_dist = Distribution.from_values([v * 1000.0 for v in gap_values])

    step("actors")
    actors = ActorStats()
    ares = _safe(db, _SQL_ASSIGNEES, limit=10_000, name="landscape_assignees",
                 timeout_ms=timeout_ms)
    tres = _safe(db, _SQL_DEFS_WITH_TASKS, limit=10_000, name="landscape_tasks",
                 timeout_ms=timeout_ms)
    if ares is not None:
        rows = ares.dicts()
        assigned = [(r["assignee"], r["n"]) for r in rows if r["assignee"]]
        unassigned = sum(r["n"] for r in rows if not r["assignee"])
        with_tasks = {r["proc_def_key_"] for r in (tres.dicts() if tres is not None else [])}
        all_defs = len(only_root) + len(only_child) + len(both)
        actors = ActorStats(
            distinct_assignees=len(assigned),
            assigned_tasks=sum(n for _, n in assigned),
            unassigned_tasks=unassigned,
            per_assignee=tuple(sorted(assigned, key=lambda t: -t[1])[:50]),
            definitions_with_human_tasks=len(with_tasks),
            definitions_fully_automated=max(0, all_defs - len(with_tasks)),
            start_users_available=False)

    step("validation-only runs across the whole history")
    validation_total: int | None = None
    vres = _safe(db, _SQL_VALIDATION_TOTAL, {"name": "onlyValidation"}, limit=2,
                 name="landscape_validation", timeout_ms=timeout_ms)
    if vres is not None and vres.rows:
        validation_total = int(vres.rows[0][0] or 0)

    # -- caveats that belong on the page -------------------------------------------------
    hist_inc = op_log = False
    if detection is not None:
        try:
            hist_inc = detection.has(Feature.HISTORIC_INCIDENTS)
            op_log = detection.has(Feature.OPERATION_LOG)
        except Exception:  # noqa: BLE001
            pass
    if not hist_inc:
        notes.append("Historic incidents are not recorded (act_hi_incident is empty at history "
                     "level AUDIT). The incident column shows open incidents of running "
                     "instances, not the history.")
    if not op_log:
        notes.append("Manual interventions are not recorded (act_hi_op_log is empty): the "
                     "externally terminated instances carry neither a reason nor an actor.")
    notes.append("The transition count is not a process model. No model exists at case level -- "
                 "what is counted is how often an ordering was observed.")
    if validation_total:
        notes.append(f"{validation_total:,} instances are validation-only runs (the "
                     "onlyValidation input parameter) and are included in every frequency "
                     "counted here.")

    total_instances = sum(v for _, v in depth_distribution) or sum(m.instances for m in monthly)
    meta = LandscapeMeta(
        built_at=datetime.now(timezone.utc),
        duration_ms=int((time.perf_counter() - started) * 1000),
        profile_name=profile.name,
        window_start=window_start, window_end=window_end,
        instances_total=total_instances,
        root_instances=sum(len(v) for v in per_key.values()),
        keys_total=len(per_key),
        definitions_total=len(only_root) + len(only_child) + len(both),
        notes=tuple(notes))

    return Landscape(
        monthly=tuple(monthly),
        call_edges=tuple(call_edges),
        depth_distribution=tuple(depth_distribution),
        only_root=tuple(sorted(only_root)),
        only_child=tuple(sorted(only_child)),
        both_roles=tuple(sorted(both)),
        co_occurrence=tuple(co_occurrence),
        transitions=tuple(
            Transition(from_def=a, to_def=b, count=n, keys=len(transition_keys[(a, b)]),
                       median_gap_ms=(int(sorted(transition_gaps[(a, b)])[len(transition_gaps[(a, b)]) // 2] * 1000)
                                      if transition_gaps.get((a, b)) else None))
            for (a, b), n in transitions.most_common(TOP_TRANSITIONS)),
        entry_defs=tuple(entries.most_common(50)),
        exit_defs=tuple(exits.most_common(50)),
        sequences=tuple(
            SequencePattern(sequence=seq, count=n, example_keys=tuple(sequence_keys[seq]))
            for seq, n in sequences.most_common(TOP_SEQUENCES)),
        sequences_distinct=len(sequences),
        sequences_unique_once=sum(1 for n in sequences.values() if n == 1),
        instances_per_key=instances_per_key,
        definitions_per_key=definitions_per_key,
        span_per_key_ms=span_dist,
        gaps_ms=gaps_dist,
        gap_counts=tuple((key, gap_bucket_counts.get(key, 0))
                         for key, _label, _limit in GAP_BUCKETS),
        overlap_pairs=overlap_pairs,
        gap_pairs=gap_pairs,
        actors=actors,
        disruptions=DisruptionStats(per_definition=tuple(disruption_rows[:200]),
                                    historic_incidents_available=hist_inc,
                                    operation_log_available=op_log),
        validation_only_instances=validation_total,
        meta=meta,
    )
