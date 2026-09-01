"""The case: everything that happened on one business key.

The heart of the tool. A business key goes in, and what happened to that business object comes
out -- chronologically, including the gaps in between.

Three decisions shape this module:

1. **The closure runs over ``root_proc_inst_id_``**, not over a recursive CTE. Measured on a
   real history: two orders of magnitude faster, and identical instance sets across hundreds of
   sampled keys. That works because the engine keeps ``root_proc_inst_id_`` consistently
   populated. ``verify_closure_equivalence()`` checks that claim against the recursive variant:
   on a database where the field is patchy -- an older engine, a migrated history -- the tool
   has to say so rather than quietly showing an incomplete timeline.

2. **The origin of every instance stays visible.** Instances started through call activities
   inherit the business key only when the model says so. Instances without a key, and instances
   with a *different* key, belong to the case but are marked apart.

3. **Foreign keys are not merged in.** A different key usually carries further instances that
   belong to another chain. They are counted and linked, not added.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from ..config import Profile
from ..contracts import (
    BusinessKeyHit,
    BusinessKeySummary,
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
from .connection import Database, DatabaseError

log = logging.getLogger("cib7explorer.db.case")

#: Upper bound on the instances of one case. A single key can carry tens of thousands of
#: instances -- reused test keys do that -- and the view still has to render. When the bound is
#: reached, the case says so through ``instances_total`` and a note. Never truncate silently.
MAX_INSTANCES = 4000

#: Above this many root instances the timeline switches to a density band instead of one lane
#: per tree; below it, a plain axis stays readable.
DENSITY_THRESHOLD = 50

#: Only values of this type are used for correlation. The filter lets PostgreSQL use the
#: existing (name_, var_type_) index, which cut the query time by roughly a factor of four.
CORRELATION_VAR_TYPE = "string"

MAX_CORRELATION_OUTSIDE = 200

_INSTANCE_COLUMNS = """
    p.proc_inst_id_, p.business_key_, p.proc_def_key_, p.proc_def_id_,
    p.start_time_, p.end_time_, p.duration_, p.state_, p.delete_reason_,
    p.start_act_id_, p.end_act_id_, p.super_process_instance_id_, p.root_proc_inst_id_,
    p.removal_time_, p.restarted_proc_inst_id_
"""

# --- SQL ------------------------------------------------------------------------------------

#: The closure: two index lookups (act_idx_hi_pro_i_buskey, act_idx_hi_pro_inst_root_pi).
_SQL_CLOSURE = f"""
    SELECT {_INSTANCE_COLUMNS}
      FROM act_hi_procinst p
     WHERE p.root_proc_inst_id_ IN (
             SELECT r.root_proc_inst_id_
               FROM act_hi_procinst r
              WHERE r.business_key_ = %(key)s)
     ORDER BY p.start_time_, p.proc_inst_id_
"""

#: Counter-check for the closure -- bidirectionally recursive over the parent relation. For
#: tests and the self-check only, never for the view: roughly a hundred times slower.
_SQL_CLOSURE_RECURSIVE = """
    WITH RECURSIVE cl AS (
        SELECT proc_inst_id_, super_process_instance_id_
          FROM act_hi_procinst
         WHERE business_key_ = %(key)s
        UNION
        SELECT n.proc_inst_id_, n.super_process_instance_id_
          FROM cl
          JOIN act_hi_procinst n
            ON n.super_process_instance_id_ = cl.proc_inst_id_
            OR n.proc_inst_id_ = cl.super_process_instance_id_
    )
    SELECT DISTINCT proc_inst_id_ FROM cl
"""

#: Versions of the definitions involved.
_SQL_VERSIONS = """
    SELECT id_, version_, key_, name_
      FROM act_re_procdef
     WHERE id_ = ANY(%(ids)s)
"""

#: Start activity per instance. The join goes through the instance's ``start_act_id_`` and NOT
#: through the activity type: embedded subprocesses have start events of their own, so a type
#: filter would count those too and attribute the wrong trigger to the instance.
_SQL_START_ACTIVITY = """
    SELECT a.proc_inst_id_, a.act_id_, a.act_type_, a.act_name_
      FROM act_hi_actinst a
      JOIN act_hi_procinst p
        ON p.proc_inst_id_ = a.proc_inst_id_
       AND a.act_id_ = p.start_act_id_
     WHERE a.proc_inst_id_ = ANY(%(ids)s)
"""

#: The ``onlyValidation`` input parameter per instance. Only the value at instance start
#: (``act_inst_id_ = proc_inst_id_``) counts: some processes set the same variable internally
#: later on, where it means something else entirely.
_SQL_VALIDATION_PER_INSTANCE = """
    SELECT proc_inst_id_,
           bool_and(
               CASE WHEN var_type_ = 'boolean' AND long_ = 1              THEN true
                    WHEN var_type_ = 'boolean' AND long_ = 0              THEN false
                    WHEN var_type_ = 'string'  AND lower(text_) = 'true'  THEN true
                    WHEN var_type_ = 'string'  AND lower(text_) = 'false' THEN false
               END) AS flag
      FROM act_hi_varinst
     WHERE proc_inst_id_ = ANY(%(ids)s)
       AND name_ = %(name)s
       AND act_inst_id_ = proc_inst_id_
     GROUP BY 1
"""

#: Open incidents per instance. ``act_hi_incident`` is empty at history level AUDIT, so this is
#: the runtime state only -- and the view labels it as such.
_SQL_OPEN_INCIDENTS = """
    SELECT proc_inst_id_, count(*) AS n, min(incident_type_) AS kind
      FROM act_ru_incident
     WHERE proc_inst_id_ = ANY(%(ids)s)
     GROUP BY 1
"""

_SQL_USER_TASKS = """
    SELECT proc_inst_id_, count(*) AS n
      FROM act_hi_taskinst
     WHERE proc_inst_id_ = ANY(%(ids)s)
     GROUP BY 1
"""

#: How many instances a foreign key carries in total -- the number that shows what is NOT part
#: of this case.
_SQL_KEY_TOTALS = """
    SELECT business_key_, count(*) AS n, min(start_time_) AS first_start
      FROM act_hi_procinst
     WHERE business_key_ = ANY(%(keys)s)
     GROUP BY 1
"""

#: Left edge of the retained history -- needed to answer whether a case is cut off at the start.
_SQL_HISTORY_START = "SELECT min(start_time_) AS first_start FROM act_hi_procinst"

_SQL_SEARCH = """
    SELECT business_key_,
           count(*) AS instance_count,
           count(DISTINCT proc_def_key_) AS definitions,
           min(start_time_) AS first_start,
           max(coalesce(end_time_, start_time_)) AS last_activity
      FROM act_hi_procinst
     WHERE business_key_ IS NOT NULL
       AND business_key_ ILIKE %(pattern)s
     GROUP BY 1
     ORDER BY count(*) DESC, business_key_
"""

#: Browse list. The sort order comes from an allowlist, never from user input.
_SQL_BROWSE = """
    SELECT business_key_,
           count(*) AS instance_count,
           count(*) FILTER (WHERE super_process_instance_id_ IS NULL) AS root_count,
           count(DISTINCT proc_def_key_) AS definitions,
           min(start_time_) AS first_start,
           max(coalesce(end_time_, start_time_)) AS last_activity,
           count(*) FILTER (WHERE end_time_ IS NULL) AS running,
           count(*) FILTER (WHERE state_ LIKE '%%TERMINATED') AS terminated_count,
           extract(epoch FROM max(coalesce(end_time_, start_time_)) - min(start_time_)) AS span_seconds
      FROM act_hi_procinst
     WHERE business_key_ IS NOT NULL
     GROUP BY 1
"""

#: Keys with open incidents -- a query of its own, because ``act_ru_incident`` knows only
#: ``proc_def_id_`` and ``proc_inst_id_``, and joining it would make the browse list expensive.
_SQL_BROWSE_INCIDENTS = """
    SELECT p.business_key_, count(*) AS open_count
      FROM act_ru_incident i
      JOIN act_hi_procinst p ON p.proc_inst_id_ = i.proc_inst_id_
     WHERE p.business_key_ IS NOT NULL
     GROUP BY 1
     ORDER BY count(*) DESC
"""

#: Second track: which instances carry the same value of an object-id variable.
_SQL_CORRELATION_VALUES = """
    SELECT DISTINCT name_, text_
      FROM act_hi_varinst
     WHERE proc_inst_id_ = ANY(%(ids)s)
       AND name_ = ANY(%(names)s)
       AND var_type_ = %(vtype)s
       AND text_ IS NOT NULL
       AND length(text_) BETWEEN 1 AND 200
"""

_SQL_CORRELATION_MATCHES = """
    SELECT v.name_, v.text_, v.proc_inst_id_, p.proc_def_key_
      FROM act_hi_varinst v
      JOIN act_hi_procinst p ON p.proc_inst_id_ = v.proc_inst_id_
     WHERE v.name_ = %(name)s
       AND v.var_type_ = %(vtype)s
       AND v.text_ = %(value)s
"""


# --- helpers --------------------------------------------------------------------------------

def _safe(db: Database, sql: str, params: Any = None, *, limit: int, name: str,
          timeout_ms: int | None = None):
    try:
        return db.fetch(sql, params, limit=limit, name=name, timeout_ms=timeout_ms)
    except DatabaseError as exc:
        log.info("sub-query '%s' failed: %s", name, exc)
        return None


def _ms(delta) -> int | None:
    return None if delta is None else int(delta.total_seconds() * 1000)


def _derive_trigger(row: dict[str, Any], start_act: dict[str, Any] | None) -> tuple[StartTrigger, str | None]:
    """Derive the start trigger from what the data says -- and claim nothing beyond that.

    ``start_user_id_`` is frequently NULL throughout an installation, so "started by a user" is
    usually not evidenced and is not claimed. When nothing is left, the answer is ``UNKNOWN``
    rather than a plausible-sounding "API".
    """
    if row.get("super_process_instance_id_"):
        return StartTrigger.PARENT_PROCESS, "through a call activity of the parent process"
    if row.get("restarted_proc_inst_id_"):
        return StartTrigger.RESTART, f"restart of {row['restarted_proc_inst_id_']}"
    act_type = (start_act or {}).get("act_type_") or ""
    act_id = (start_act or {}).get("act_id_")
    mapping = {
        "signalStartEvent": StartTrigger.SIGNAL,
        "messageStartEvent": StartTrigger.MESSAGE,
        "timerStartEvent": StartTrigger.TIMER,
        "conditionalStartEvent": StartTrigger.CONDITIONAL,
        "startEvent": StartTrigger.PLAIN_START,
        "noneStartEvent": StartTrigger.PLAIN_START,
    }
    trigger = mapping.get(act_type)
    if trigger is None:
        return StartTrigger.UNKNOWN, (f"start activity {act_id}" if act_id else None)
    detail = f"{act_type} '{act_id}'" if act_id else act_type
    if trigger is StartTrigger.PLAIN_START:
        detail += " -- no event type recorded, so triggered from outside (API); "
        detail += "who triggered it is not in the data"
    return trigger, detail


# --- search and browse ----------------------------------------------------------------------

def search_keys(db: Database, term: str, *, mode: str = "contains", limit: int = 200,
                timeout_ms: int | None = None) -> list[BusinessKeyHit]:
    """Search business keys by substring or prefix."""
    term = (term or "").strip()
    if not term:
        return []
    pattern = f"{term}%" if mode == "prefix" else f"%{term}%"
    res = _safe(db, _SQL_SEARCH, {"pattern": pattern}, limit=limit, name="case_search",
                timeout_ms=timeout_ms)
    if res is None:
        return []
    return [BusinessKeyHit(key=r["business_key_"], instances=r["instance_count"],
                           definitions=r["definitions"], first_start=r["first_start"],
                           last_activity=r["last_activity"])
            for r in res.dicts()]


#: Allowlist of sort orders for the browse list. Each label doubles as the reason the order is
#: offered at all: this list is how interesting cases are found without knowing a single key.
BROWSE_ORDERS: dict[str, str] = {
    "instances": "most process instances",
    "definitions": "most distinct process definitions",
    "span": "longest total duration",
    "last_active": "most recently active",
    "running": "instances still running",
    "incidents": "open incidents",
}

_BROWSE_SORT_KEY = {
    "instances": lambda s: (s.instances, s.definitions),
    "definitions": lambda s: (s.definitions, s.instances),
    "span": lambda s: (s.span_ms or 0, s.instances),
    "last_active": lambda s: (s.last_activity.timestamp() if s.last_activity else 0.0,),
    "running": lambda s: (s.running, s.instances),
    "incidents": lambda s: (s.open_incidents, s.instances),
}


def browse_keys(db: Database, *, order: str = "instances", limit: int = 50,
                timeout_ms: int | None = None) -> list[BusinessKeySummary]:
    """The browse list: one row per business key, sorted by the requested order."""
    if order not in BROWSE_ORDERS:
        order = "instances"
    res = _safe(db, _SQL_BROWSE, limit=50_000, name="case_browse", timeout_ms=timeout_ms)
    if res is None:
        return []

    incidents: dict[str, int] = {}
    inc = _safe(db, _SQL_BROWSE_INCIDENTS, limit=10_000, name="case_browse_incidents",
                timeout_ms=timeout_ms)
    if inc is not None:
        incidents = {r["business_key_"]: r["open_count"] for r in inc.dicts()}

    rows = [
        BusinessKeySummary(
            key=r["business_key_"], instances=r["instance_count"], root_instances=r["root_count"],
            definitions=r["definitions"], first_start=r["first_start"], last_activity=r["last_activity"],
            running=r["running"], terminated=r["terminated_count"],
            open_incidents=incidents.get(r["business_key_"], 0),
            span_ms=int((r["span_seconds"] or 0) * 1000),
        )
        for r in res.dicts()
    ]
    rows.sort(key=_BROWSE_SORT_KEY[order], reverse=True)
    return rows[:limit]


# --- the case -------------------------------------------------------------------------------

_SQL_CLOSURE_COUNT = """
    SELECT count(*) AS n
      FROM act_hi_procinst p
     WHERE p.root_proc_inst_id_ IN (
             SELECT r.root_proc_inst_id_
               FROM act_hi_procinst r
              WHERE r.business_key_ = %(key)s)
"""


def load_case(db: Database, profile: Profile, key: str, *,
                 max_instances: int = MAX_INSTANCES,
                 validation_variable: str = "onlyValidation",
                 timeout_ms: int | None = None) -> Case:
    """Load everything that happened on this business key.

    The closure covers the instances carrying the key **and** the complete parent/child chain
    around them. Foreign keys are counted and linked, never merged in.
    """
    started = time.perf_counter()
    notes: list[CaseNote] = []

    res = _safe(db, _SQL_CLOSURE, {"key": key}, limit=max_instances,
                name="case_closure", timeout_ms=timeout_ms)
    if res is None:
        return Case(key=key, loaded_at=datetime.now(timezone.utc), notes=(
            CaseNote("warn", "The closure could not be loaded -- see the log."),))
    rows = res.dicts()
    if not rows:
        return Case(key=key, loaded_at=datetime.now(timezone.utc), notes=(
            CaseNote("info", f"There is no process instance for '{key}' in this history. That "
                                "may also mean it was removed by history cleanup -- the key "
                                "itself is not retained anywhere."),))

    total = len(rows)
    if res.truncated:
        cnt = _safe(db, _SQL_CLOSURE_COUNT, {"key": key}, limit=1, name="case_count",
                    timeout_ms=timeout_ms)
        if cnt is not None and cnt.rows:
            total = int(cnt.rows[0][0])
        notes.append(CaseNote(
            "warn",
            f"This case covers {total:,} instances; the first {len(rows):,} by start time are "
            "shown. The timeline is therefore incomplete."))

    ids = [r["proc_inst_id_"] for r in rows]
    def_ids = sorted({r["proc_def_id_"] for r in rows if r["proc_def_id_"]})

    versions: dict[str, dict[str, Any]] = {}
    vres = _safe(db, _SQL_VERSIONS, {"ids": def_ids}, limit=max(len(def_ids) + 1, 10),
                 name="case_versions", timeout_ms=timeout_ms)
    if vres is not None:
        versions = {r["id_"]: r for r in vres.dicts()}

    start_acts: dict[str, dict[str, Any]] = {}
    ares = _safe(db, _SQL_START_ACTIVITY, {"ids": ids}, limit=max_instances * 2,
                 name="case_start_activity", timeout_ms=timeout_ms)
    if ares is not None:
        for r in ares.dicts():
            start_acts.setdefault(r["proc_inst_id_"], r)

    validation: dict[str, bool | None] = {}
    valres = _safe(db, _SQL_VALIDATION_PER_INSTANCE,
                   {"ids": ids, "name": validation_variable}, limit=max_instances + 1,
                   name="case_validation", timeout_ms=timeout_ms)
    if valres is not None:
        validation = {r["proc_inst_id_"]: r["flag"] for r in valres.dicts()}

    incidents: dict[str, int] = {}
    ires = _safe(db, _SQL_OPEN_INCIDENTS, {"ids": ids}, limit=max_instances + 1,
                 name="case_incidents", timeout_ms=timeout_ms)
    if ires is not None:
        incidents = {r["proc_inst_id_"]: r["n"] for r in ires.dicts()}

    tasks: dict[str, int] = {}
    tres = _safe(db, _SQL_USER_TASKS, {"ids": ids}, limit=max_instances + 1,
                 name="case_user_tasks", timeout_ms=timeout_ms)
    if tres is not None:
        tasks = {r["proc_inst_id_"]: r["n"] for r in tres.dicts()}

    # -- build the nodes -----------------------------------------------------------------
    present = set(ids)
    children: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        parent = r["super_process_instance_id_"]
        if parent:
            children[parent].append(r["proc_inst_id_"])

    #: An identical start time means an undetermined order. ``act_hi_procinst`` has no
    #: sequence counter, so nothing in the data could decide it.
    by_start: dict[Any, int] = defaultdict(int)
    for r in rows:
        by_start[r["start_time_"]] += 1

    nodes: list[InstanceNode] = []
    for r in rows:
        pid = r["proc_inst_id_"]
        parent = r["super_process_instance_id_"]
        bk = r["business_key_"]
        if bk == key:
            origin = InstanceOrigin.OWN_KEY
        elif bk is None:
            origin = InstanceOrigin.NO_KEY
        else:
            origin = InstanceOrigin.OTHER_KEY
        trigger, detail = _derive_trigger(r, start_acts.get(pid))
        ver = versions.get(r["proc_def_id_"]) or {}
        nodes.append(InstanceNode(
            proc_inst_id=pid,
            def_key=r["proc_def_key_"] or "(no definition key)",
            def_id=r["proc_def_id_"],
            parent_id=parent,
            root_id=r["root_proc_inst_id_"],
            version=ver.get("version_"),
            business_key=bk,
            origin=origin,
            start_time=r["start_time_"],
            end_time=r["end_time_"],
            duration_ms=r["duration_"],
            state=r["state_"],
            delete_reason=r["delete_reason_"],
            start_act_id=r["start_act_id_"],
            end_act_id=r["end_act_id_"],
            removal_time=r["removal_time_"],
            restarted_from=r["restarted_proc_inst_id_"],
            start_trigger=trigger,
            start_trigger_detail=detail,
            child_ids=tuple(children.get(pid, ())),
            validation_only=validation.get(pid),
            open_incidents=incidents.get(pid, 0),
            user_task_count=tasks.get(pid),
            order_ambiguous=by_start[r["start_time_"]] > 1,
            orphaned_parent=bool(parent and parent not in present),
        ))

    nodes = _with_depth(nodes)
    trees = _build_trees(nodes)
    gaps, open_end = _build_gaps(trees)
    foreign = _foreign_keys(db, key, nodes, timeout_ms=timeout_ms)
    notes.extend(_build_notes(db, key, nodes, trees, gaps, foreign, timeout_ms=timeout_ms))

    window_start = min((n.start_time for n in nodes if n.start_time), default=None)
    ends = [n.end_time for n in nodes if n.end_time]
    window_end = max(ends) if ends else None
    if open_end:
        window_end = max([window_end] + [n.start_time for n in nodes if n.is_running and n.start_time]) \
            if window_end else max((n.start_time for n in nodes if n.start_time), default=None)

    hist_start = None
    hres = _safe(db, _SQL_HISTORY_START, limit=1, name="case_history_start",
                 timeout_ms=timeout_ms)
    if hres is not None and hres.rows:
        hist_start = hres.rows[0][0]

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return Case(
        key=key,
        instances=tuple(nodes),
        trees=tuple(trees),
        gaps=tuple(gaps),
        foreign_keys=tuple(foreign),
        notes=tuple(notes),
        window_start=window_start,
        window_end=window_end,
        history_first_start=hist_start,
        truncated_left=bool(hist_start and window_start and window_start <= hist_start),
        open_end=open_end,
        partially_removable=any(n.removal_time and n.removal_time < now for n in nodes),
        instances_shown=len(nodes),
        instances_total=total,
        trees_total=len(trees),
        loaded_at=datetime.now(timezone.utc),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def _with_depth(nodes: Sequence[InstanceNode]) -> list[InstanceNode]:
    """Assign the call depth. An orphan -- a node whose parent is not in the closure -- is kept
    at depth 0 and keeps its marker: it is displayed, not dropped."""
    import dataclasses

    by_id = {n.proc_inst_id: n for n in nodes}
    cache: dict[str, int] = {}

    def depth_of(pid: str, guard: int = 0) -> int:
        if pid in cache:
            return cache[pid]
        node = by_id.get(pid)
        if node is None or node.parent_id is None or node.parent_id not in by_id or guard > 40:
            cache[pid] = 0
            return 0
        cache[pid] = depth_of(node.parent_id, guard + 1) + 1
        return cache[pid]

    return [dataclasses.replace(n, depth=depth_of(n.proc_inst_id)) for n in nodes]


def _build_trees(nodes: Sequence[InstanceNode]) -> list[TreeSummary]:
    """Summarise per root instance. The tree, not the instance, is the unit of the timeline --
    a busy case holds far more instances than trees, and a per-instance timeline stops being
    readable long before a per-tree one does."""
    by_root: dict[str, list[InstanceNode]] = defaultdict(list)
    for n in nodes:
        by_root[n.root_id or n.proc_inst_id].append(n)

    trees: list[TreeSummary] = []
    for root_id, members in by_root.items():
        root = next((m for m in members if m.proc_inst_id == root_id), None) or min(
            members, key=lambda m: (m.depth, m.start_time or datetime.max))
        starts = [m.start_time for m in members if m.start_time]
        ends = [m.end_time for m in members if m.end_time]
        running = any(m.is_running for m in members)
        flags = {m.validation_only for m in members if m.validation_only is not None}
        trees.append(TreeSummary(
            root_id=root_id,
            def_key=root.def_key,
            start_time=min(starts) if starts else None,
            end_time=None if running else (max(ends) if ends else None),
            instance_count=len(members),
            max_depth=max(m.depth for m in members),
            instance_ids=tuple(m.proc_inst_id for m in members),
            business_keys=tuple(sorted({m.business_key for m in members if m.business_key})),
            validation_only=(root.validation_only if root.validation_only is not None
                             else (True if flags == {True} else (False if flags == {False} else None))),
            open_incidents=sum(m.open_incidents for m in members),
            running=running,
            terminated=any(m.terminated for m in members),
        ))
    trees.sort(key=lambda t: (t.start_time or datetime.max, t.root_id))
    return trees


def _build_gaps(trees: Sequence[TreeSummary]) -> tuple[list[Gap], bool]:
    """Gaps and overlaps between the call trees.

    Computed between trees, not between all instances: subprocesses run inside their parents and
    would paper over every gap. A tree's start is compared against the running **high-water
    mark** of all end times so far, not against the immediately preceding tree -- otherwise a
    short tree running inside a long one reports a gap that does not exist.

    A negative difference is not a gap but parallelism, and it is reported as such. Overlaps are
    common in practice, so calling them gaps would be actively misleading.
    """
    gaps: list[Gap] = []
    open_end = any(t.running for t in trees)
    hwm: datetime | None = None          # high-water mark of end times
    hwm_tree: TreeSummary | None = None
    still_running: TreeSummary | None = None

    for tree in trees:
        if hwm_tree is not None and tree.start_time is not None:
            if still_running is not None:
                # An earlier tree is still running: there is no period without a process.
                gaps.append(Gap(
                    kind=GapKind.OVERLAP, start=tree.start_time, end=None, duration_ms=None,
                    after_root=still_running.root_id, before_root=tree.root_id,
                    after_def=still_running.def_key, before_def=tree.def_key))
            elif hwm is not None:
                delta_ms = _ms(tree.start_time - hwm)
                if delta_ms is not None and delta_ms > 0:
                    gaps.append(Gap(
                        kind=GapKind.BETWEEN, start=hwm, end=tree.start_time,
                        duration_ms=delta_ms,
                        after_root=hwm_tree.root_id, before_root=tree.root_id,
                        after_def=hwm_tree.def_key, before_def=tree.def_key))
                elif delta_ms is not None and delta_ms < 0:
                    gaps.append(Gap(
                        kind=GapKind.OVERLAP, start=tree.start_time, end=hwm,
                        duration_ms=-delta_ms,
                        after_root=hwm_tree.root_id, before_root=tree.root_id,
                        after_def=hwm_tree.def_key, before_def=tree.def_key))

        if tree.running:
            still_running = still_running or tree
        if tree.end_time is not None and (hwm is None or tree.end_time > hwm):
            hwm, hwm_tree = tree.end_time, tree
        elif hwm_tree is None:
            hwm_tree = tree

    return gaps, open_end


def _foreign_keys(db: Database, key: str, nodes: Sequence[InstanceNode], *,
                  timeout_ms: int | None = None) -> list[ForeignKeyLink]:
    """Different business keys inside the closure, together with their total instance count.

    The total is the whole point: it shows how much hangs off that key which does NOT belong to
    this case. Typically a foreign key carries roughly twice as many instances as the ones
    reachable from here. Hence a link to jump, not a silent expansion of the case.
    """
    per_key: dict[str, list[InstanceNode]] = defaultdict(list)
    for n in nodes:
        if n.origin is InstanceOrigin.OTHER_KEY and n.business_key:
            per_key[n.business_key].append(n)
    if not per_key:
        return []

    totals: dict[str, dict[str, Any]] = {}
    res = _safe(db, _SQL_KEY_TOTALS, {"keys": sorted(per_key)}, limit=len(per_key) + 1,
                name="case_foreign_keys", timeout_ms=timeout_ms)
    if res is not None:
        totals = {r["business_key_"]: r for r in res.dicts()}

    links = [
        ForeignKeyLink(
            key=fk,
            instances_in_case=len(members),
            instances_total=(totals.get(fk) or {}).get("n"),
            first_seen=min((m.start_time for m in members if m.start_time), default=None),
            via_definitions=tuple(sorted({m.def_key for m in members})),
        )
        for fk, members in per_key.items()
    ]
    links.sort(key=lambda l: (-l.instances_in_case, l.key))
    return links


def _build_notes(db: Database, key: str, nodes: Sequence[InstanceNode],
                 trees: Sequence[TreeSummary], gaps: Sequence[Gap],
                 foreign: Sequence[ForeignKeyLink], *,
                 timeout_ms: int | None = None) -> list[CaseNote]:
    """The caveats that belong on the page itself rather than in documentation nobody opens."""
    notes: list[CaseNote] = []
    n_no_key = sum(1 for n in nodes if n.origin is InstanceOrigin.NO_KEY)
    n_other = sum(1 for n in nodes if n.origin is InstanceOrigin.OTHER_KEY)

    if n_no_key:
        notes.append(CaseNote(
            "info",
            f"{n_no_key} of {len(nodes)} instances do not carry the key '{key}' themselves and "
            "belong to the case through their parent chain. Instances started through call "
            "activities inherit the business key only when the model says so."))
    if n_other:
        keys = ", ".join(f.key for f in foreign[:5])
        notes.append(CaseNote(
            "info",
            f"{n_other} instances carry a different business key ({keys}"
            f"{' ...' if len(foreign) > 5 else ''}). That is a key change inside the call chain, "
            "and it usually means something in the domain."))

    orphans = [n for n in nodes if n.orphaned_parent]
    if orphans:
        notes.append(CaseNote(
            "warn",
            f"{len(orphans)} instances point to a parent process that is not (or no longer) "
            "present in this history. They are shown as orphans rather than dropped -- their "
            "beginning lies outside what is visible here."))

    ambiguous = sum(1 for n in nodes if n.order_ambiguous)
    if ambiguous:
        notes.append(CaseNote(
            "info",
            f"{ambiguous} instances share their start timestamp with another. Their order is "
            "undetermined: act_hi_procinst keeps no sequence counter, so nothing in the data "
            "could decide it."))

    running = sum(1 for n in nodes if n.is_running)
    if running:
        notes.append(CaseNote(
            "info",
            f"{running} instances are still running. The end of this case is open -- the right "
            "edge of the timeline is not a conclusion."))

    unknown_trigger = sum(1 for n in nodes if n.start_trigger is StartTrigger.UNKNOWN)
    if unknown_trigger:
        notes.append(CaseNote(
            "info",
            f"For {unknown_trigger} instances the start trigger cannot be derived from the "
            "data. start_user_id_ is empty throughout this history, so a start by a named user "
            "cannot be evidenced here at all."))

    val_unknown = sum(1 for n in nodes if n.validation_only is None)
    val_true = sum(1 for n in nodes if n.validation_only)
    if val_true:
        notes.append(CaseNote(
            "info",
            f"{val_true} of {len(nodes)} instances are validation-only runs (the onlyValidation "
            "input parameter at instance start). Nothing happened in the domain there, even "
            "though the timeline shows activity."))
    if val_unknown == len(nodes) and nodes:
        notes.append(CaseNote(
            "info",
            "No instance of this case carries the onlyValidation input parameter -- whether any "
            "of them were validation runs is not in this data."))

    if any(n.removal_time for n in nodes):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        due = [n for n in nodes if n.removal_time and n.removal_time < now]
        if due:
            notes.append(CaseNote(
                "warn",
                f"{len(due)} instances of this case are already past their removal_time_: "
                "history cleanup is allowed to delete them. What is missing here may already "
                "have been removed."))
    return notes


# --- second track: correlation over variable values -----------------------------------------

def correlate(db: Database, profile: Profile, case: Case, *,
              variables: Iterable[str] | None = None,
              timeout_ms: int | None = None) -> list[Correlation]:
    """Instances carrying the same value of an object-id variable as this case does.

    A separate track, never merged into the case itself. Plain counting -- "these 36 instances
    carry ticketNumber = X" -- and never a claim that two identically named variables mean the
    same thing.

    This reads variable values and therefore depends on the value mode. The filter on
    ``var_type_ = 'string'`` lets PostgreSQL use the existing (name_, var_type_) index, which is
    the difference between a fast query and a slow one.
    """
    if not case.instances:
        return []
    names = list(variables if variables is not None else profile.correlation_variables)
    if not names:
        return []
    ids = [n.proc_inst_id for n in case.instances]
    inside = set(ids)

    vals = _safe(db, _SQL_CORRELATION_VALUES,
                 {"ids": ids, "names": names, "vtype": CORRELATION_VAR_TYPE},
                 limit=500, name="correlation_values", timeout_ms=timeout_ms)
    if vals is None:
        return []

    out: list[Correlation] = []
    for row in vals.dicts():
        name, value = row["name_"], row["text_"]
        res = _safe(db, _SQL_CORRELATION_MATCHES,
                    {"name": name, "value": value, "vtype": CORRELATION_VAR_TYPE},
                    limit=MAX_CORRELATION_OUTSIDE + len(ids) + 1,
                    name="correlation_matches", timeout_ms=timeout_ms)
        if res is None:
            continue
        matches = res.dicts()
        matched = {m["proc_inst_id_"] for m in matches}
        outside = matched - inside
        if not outside:
            continue
        out.append(Correlation(
            variable=name,
            value_shown=value,
            instances_in_case=len(matched & inside),
            instances_total=len(matched),
            outside_ids=tuple(sorted(outside)[:MAX_CORRELATION_OUTSIDE]),
            outside_def_keys=tuple(sorted({m["proc_def_key_"] for m in matches
                                           if m["proc_inst_id_"] in outside and m["proc_def_key_"]})),
            truncated=res.truncated,
        ))
    out.sort(key=lambda c: (-c.instances_outside, c.variable))
    return out


# --- self-check -----------------------------------------------------------------------------

def verify_closure_equivalence(db: Database, key: str, *,
                               timeout_ms: int | None = None) -> tuple[bool, int, int]:
    """Compare the fast closure against the recursive counter-check.

    Returns ``(equal, count_via_root, count_recursive)``. The fast variant relies on
    ``root_proc_inst_id_`` being populated consistently. On a database where that does not hold
    -- an older engine, a migrated history -- the tool has to report it rather than quietly
    showing an incomplete timeline.
    """
    a = _safe(db, _SQL_CLOSURE, {"key": key}, limit=MAX_INSTANCES, name="check_via_root",
              timeout_ms=timeout_ms)
    b = _safe(db, _SQL_CLOSURE_RECURSIVE, {"key": key}, limit=MAX_INSTANCES,
              name="check_recursive", timeout_ms=timeout_ms)
    if a is None or b is None:
        return False, -1, -1
    ids_a = {r[0] for r in a.rows}
    ids_b = {r[0] for r in b.rows}
    return ids_a == ids_b, len(ids_a), len(ids_b)
