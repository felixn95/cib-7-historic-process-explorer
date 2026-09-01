"""One process instance in detail.

What has to stay honest here: at history level AUDIT there is no claim timestamp for user tasks
(it would live in ``act_hi_op_log``, which is empty) and no variable change history (that would
be ``act_hi_detail``, also empty). Splitting a task's elapsed time into waiting time and working
time is therefore **not computable** on such data -- so it is shown as missing rather than
estimated. An estimate would look like an answer and be a guess.

Variable values go exclusively through ``cib7explorer.values``. Large and binary values are
never loaded automatically.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from ..config import Profile
from ..contracts import (
    ActivityRun,
    Feature,
    InstanceDetail,
    InstanceNode,
    InstanceOrigin,
    StartTrigger,
    TaskRun,
    VariableEntry,
    CaseNote,
)
from ..values import AUTO_LOAD_MAX_BYTES, REQUEST_MAX_BYTES, ValueAccess, resolve_access
from .connection import Database, DatabaseError

log = logging.getLogger("cib7explorer.db.instance")

MAX_ACTIVITIES = 5000
MAX_VARIABLES = 2000

#: Length of the value preview in the variable table. The full value is fetched only when the
#: dialog is opened, via ``load_value``. That keeps the page small and sends nothing to the
#: browser that nobody looks at.
PREVIEW_CHARS = 200

_SQL_INSTANCE = """
    SELECT p.proc_inst_id_, p.business_key_, p.proc_def_key_, p.proc_def_id_,
           p.start_time_, p.end_time_, p.duration_, p.state_, p.delete_reason_,
           p.start_act_id_, p.end_act_id_, p.super_process_instance_id_, p.root_proc_inst_id_,
           p.removal_time_, p.restarted_proc_inst_id_,
           d.version_ AS version, d.name_ AS def_name, d.deployment_id_, d.resource_name_
      FROM act_hi_procinst p
      LEFT JOIN act_re_procdef d ON d.id_ = p.proc_def_id_
     WHERE p.proc_inst_id_ = %(id)s
"""

_SQL_ACTIVITIES = """
    SELECT id_, act_id_, act_type_, act_name_, start_time_, end_time_, duration_,
           sequence_counter_, parent_act_inst_id_, task_id_, call_proc_inst_id_, assignee_,
           act_inst_state_
      FROM act_hi_actinst
     WHERE proc_inst_id_ = %(id)s
     ORDER BY start_time_, sequence_counter_, id_
"""

_SQL_TASKS = """
    SELECT id_, name_, task_def_key_, assignee_, owner_, start_time_, end_time_, duration_,
           priority_, due_date_, follow_up_date_, delete_reason_, task_state_, act_inst_id_
      FROM act_hi_taskinst
     WHERE proc_inst_id_ = %(id)s
     ORDER BY start_time_, id_
"""

#: Variables of the instance. ``text_`` is fetched here because this view may show values; what
#: is actually allowed is then decided by ``cib7explorer.values``, not by the query. Binary
#: values are NOT included: only their length, everything else on explicit request.
_SQL_VARIABLES = """
    SELECT v.id_, v.name_, v.var_type_, v.create_time_, v.act_inst_id_, v.proc_inst_id_,
           v.execution_id_, v.task_id_, v.rev_, v.state_, v.text_, v.text2_,
           v.long_, v.double_, v.bytearray_id_,
           octet_length(b.bytes_) AS byte_len,
           length(v.text_) AS text_len,
           -- Small text values (JSON/XML) come along, so they do not need an explicit request
           -- for no reason. `serializable` and `bytes` do NOT: those are Java object streams and
           -- binary data, and convert_from would fail on them.
           CASE WHEN v.var_type_ IN ('json', 'xml', 'spin://application/json')
                     AND octet_length(b.bytes_) <= %(auto)s
                THEN convert_from(b.bytes_, 'UTF8') END AS bytes_text
      FROM act_hi_varinst v
      LEFT JOIN act_ge_bytearray b ON b.id_ = v.bytearray_id_
     WHERE v.proc_inst_id_ = %(id)s
     ORDER BY v.name_
"""

_SQL_ONE_BYTEARRAY = """
    SELECT octet_length(b.bytes_) AS byte_len,
           convert_from(substring(b.bytes_ from 1 for %(limit)s), 'UTF8') AS text_value
      FROM act_ge_bytearray b
     WHERE b.id_ = %(id)s
"""

_SQL_CHILDREN = """
    SELECT proc_inst_id_, business_key_, proc_def_key_, proc_def_id_, start_time_, end_time_,
           duration_, state_, super_process_instance_id_, root_proc_inst_id_
      FROM act_hi_procinst
     WHERE super_process_instance_id_ = %(id)s
     ORDER BY start_time_
"""

_SQL_PARENT = """
    SELECT proc_inst_id_, business_key_, proc_def_key_, proc_def_id_, start_time_, end_time_,
           duration_, state_, super_process_instance_id_, root_proc_inst_id_
      FROM act_hi_procinst
     WHERE proc_inst_id_ = %(id)s
"""

_SQL_INCIDENTS = """
    SELECT incident_type_, coalesce(incident_msg_, '') AS msg, create_time_, activity_id_
      FROM act_ru_incident
     WHERE proc_inst_id_ = %(id)s
     ORDER BY create_time_
"""

_SQL_BPMN_META = """
    SELECT b.id_, b.name_, octet_length(b.bytes_) AS bytes
      FROM act_re_procdef d
      JOIN act_ge_bytearray b
        ON b.deployment_id_ = d.deployment_id_ AND b.name_ = d.resource_name_
     WHERE d.id_ = %(def_id)s
"""

_SQL_BPMN_XML = """
    SELECT convert_from(b.bytes_, 'UTF8') AS xml
      FROM act_re_procdef d
      JOIN act_ge_bytearray b
        ON b.deployment_id_ = d.deployment_id_ AND b.name_ = d.resource_name_
     WHERE d.id_ = %(def_id)s
"""


def _safe(db: Database, sql: str, params: Any = None, *, limit: int, name: str,
          timeout_ms: int | None = None):
    try:
        return db.fetch(sql, params, limit=limit, name=name, timeout_ms=timeout_ms)
    except DatabaseError as exc:
        log.info("sub-query '%s' failed: %s", name, exc)
        return None


def _node_from_row(row: dict[str, Any], *, key_of_interest: str | None = None) -> InstanceNode:
    bk = row.get("business_key_")
    if key_of_interest is None:
        origin = InstanceOrigin.OWN_KEY if bk else InstanceOrigin.NO_KEY
    elif bk == key_of_interest:
        origin = InstanceOrigin.OWN_KEY
    elif bk is None:
        origin = InstanceOrigin.NO_KEY
    else:
        origin = InstanceOrigin.OTHER_KEY
    return InstanceNode(
        proc_inst_id=row["proc_inst_id_"],
        def_key=row.get("proc_def_key_") or "(no definition key)",
        def_id=row.get("proc_def_id_"),
        parent_id=row.get("super_process_instance_id_"),
        root_id=row.get("root_proc_inst_id_"),
        version=row.get("version"),
        business_key=bk,
        origin=origin,
        start_time=row.get("start_time_"),
        end_time=row.get("end_time_"),
        duration_ms=row.get("duration_"),
        state=row.get("state_"),
        delete_reason=row.get("delete_reason_"),
        start_act_id=row.get("start_act_id_"),
        end_act_id=row.get("end_act_id_"),
        removal_time=row.get("removal_time_"),
        restarted_from=row.get("restarted_proc_inst_id_"),
    )


def _scope_label(row: dict[str, Any]) -> str:
    if row.get("task_id_"):
        return "task-local"
    if row.get("execution_id_") and row.get("execution_id_") != row.get("proc_inst_id_"):
        return "below the process instance"
    return "process instance"


def _render_value(row: dict[str, Any], access: ValueAccess, def_key: str | None
                  ) -> tuple[str | None, bool, str, bool, bool]:
    """Return ``(value, allowed, reason, truncated, on_request_only)``.

    Order of checks: the value policy first, the size second. A blocked value is not loaded
    merely because it is small -- and an allowed one is not loaded automatically merely because
    it is allowed.
    """
    name = row["name_"]
    if not access.allows(def_key, name):
        return None, False, access.why_not(def_key, name), False, False

    var_type = (row.get("var_type_") or "").lower()
    if var_type == "null":
        return None, True, "The variable exists but carries no value (type null).", False, False

    if row.get("bytearray_id_"):
        size = row.get("byte_len")
        included = row.get("bytes_text")
        if included is not None:
            if len(included) > PREVIEW_CHARS:
                return included[:PREVIEW_CHARS], True, "", True, False
            return included, True, "", False, False
        if size is not None and size > AUTO_LOAD_MAX_BYTES:
            return (None, True,
                    f"{size:,} bytes -- loaded only on explicit request.",
                    False, True)
        return (None, True,
                "The value is binary (a Java object stream or raw bytes) -- load it on request, "
                "if it is readable at all.", False, True)

    if var_type == "boolean":
        long_ = row.get("long_")
        return ("true" if long_ == 1 else "false" if long_ == 0 else None), True, "", False, False
    if var_type in ("integer", "long", "short"):
        v = row.get("long_")
        return (str(v) if v is not None else None), True, "", False, False
    if var_type == "double":
        v = row.get("double_")
        return (str(v) if v is not None else None), True, "", False, False
    if var_type == "date":
        v = row.get("long_")
        if v is None:
            return None, True, "", False, False
        return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc).isoformat(), True, "", False, False

    text = row.get("text_")
    if text is None:
        return None, True, "No text value recorded.", False, False
    if text == "":
        #: The engine additionally marks this with text2_ = '!emptyString!' -- an empty string
        #: is a value, and not the same thing as "no value".
        return "(empty string)", True, "", False, False
    if len(text) > PREVIEW_CHARS:
        return text[:PREVIEW_CHARS], True, "", True, False
    return text, True, "", False, False


def load_instance(db: Database, profile: Profile, proc_inst_id: str, *,
                  detection: Any = None, timeout_ms: int | None = None) -> InstanceDetail | None:
    """Load one process instance with its activities, tasks, variables and neighbours."""
    started = time.perf_counter()
    access = resolve_access(profile)

    res = _safe(db, _SQL_INSTANCE, {"id": proc_inst_id}, limit=2, name="instance_header",
                timeout_ms=timeout_ms)
    if res is None or not res.rows:
        return None
    head = res.dicts()[0]
    node = _node_from_row(head)
    def_key = node.def_key

    notes: list[CaseNote] = []

    acts: list[ActivityRun] = []
    ares = _safe(db, _SQL_ACTIVITIES, {"id": proc_inst_id}, limit=MAX_ACTIVITIES,
                 name="instance_activities", timeout_ms=timeout_ms)
    if ares is not None:
        rows = ares.dicts()
        if ares.truncated:
            notes.append(CaseNote("warn", f"More than {MAX_ACTIVITIES} activities -- the list "
                                          "is truncated."))
        by_start: dict[Any, int] = defaultdict(int)
        for r in rows:
            by_start[r["start_time_"]] += 1
        for r in rows:
            #: With equal timestamps, `sequence_counter_` decides the order -- act_hi_actinst
            #: keeps one, unlike act_hi_procinst. Only when that is missing or equal too does the
            #: order stay undetermined.
            concurrent = by_start[r["start_time_"]] > 1
            acts.append(ActivityRun(
                act_inst_id=r["id_"], act_id=r["act_id_"], act_type=r["act_type_"],
                act_name=r["act_name_"], start_time=r["start_time_"], end_time=r["end_time_"],
                duration_ms=r["duration_"], sequence_counter=r["sequence_counter_"],
                parent_act_inst_id=r["parent_act_inst_id_"], task_id=r["task_id_"],
                call_proc_inst_id=r["call_proc_inst_id_"], assignee=r["assignee_"],
                act_inst_state=r["act_inst_state_"],
                order_ambiguous=concurrent and r["sequence_counter_"] is None))

    #: Derive the start trigger with the same rule the case view uses, so that one instance
    #: never gets two different answers in two places.
    from dataclasses import replace as _replace

    from .case import _derive_trigger

    start_act = next((a for a in acts if a.act_id == node.start_act_id), None)
    trigger, trigger_detail = _derive_trigger(
        head, {"act_type_": start_act.act_type, "act_id_": start_act.act_id} if start_act else None)
    node = _replace(node, start_trigger=trigger, start_trigger_detail=trigger_detail)

    tasks: list[TaskRun] = []
    tres = _safe(db, _SQL_TASKS, {"id": proc_inst_id}, limit=500, name="instance_tasks",
                 timeout_ms=timeout_ms)
    lifecycle_missing = True
    if detection is not None:
        try:
            lifecycle_missing = not (detection.has(Feature.OPERATION_LOG)
                                     and detection.has(Feature.IDENTITY_LINKS))
        except Exception:  # noqa: BLE001
            lifecycle_missing = True
    if tres is not None:
        for r in tres.dicts():
            tasks.append(TaskRun(
                task_id=r["id_"], name=r["name_"], task_def_key=r["task_def_key_"],
                assignee=r["assignee_"], owner=r["owner_"], start_time=r["start_time_"],
                end_time=r["end_time_"], duration_ms=r["duration_"], priority=r["priority_"],
                due_date=r["due_date_"], follow_up_date=r["follow_up_date_"],
                delete_reason=r["delete_reason_"], task_state=r["task_state_"],
                act_inst_id=r["act_inst_id_"],
                claim_time_available=not lifecycle_missing,
                lifecycle_note=("" if not lifecycle_missing else
                                "Claim time, delegation and reassignment live in "
                                "act_hi_op_log and act_hi_identitylink, which are empty at "
                                "history level AUDIT. Waiting time and working time therefore "
                                "cannot be separated here.")))
    if tasks and lifecycle_missing:
        notes.append(CaseNote(
            "info",
            "The user tasks have no claim timestamp: splitting elapsed time into waiting and "
            "working time is not computable at history level AUDIT."))

    act_by_inst = {a.act_inst_id: a for a in acts}
    variables: list[VariableEntry] = []
    vres = _safe(db, _SQL_VARIABLES, {"id": proc_inst_id, "auto": AUTO_LOAD_MAX_BYTES},
                 limit=MAX_VARIABLES,
                 name="instance_variables", timeout_ms=timeout_ms)
    if vres is not None:
        if vres.truncated:
            notes.append(CaseNote("warn", f"More than {MAX_VARIABLES} variables -- the list "
                                          "is truncated."))
        for r in vres.dicts():
            value, allowed, reason, truncated, on_request = _render_value(r, access, def_key)
            act = act_by_inst.get(r.get("act_inst_id_"))
            variables.append(VariableEntry(
                name=r["name_"], var_type=r["var_type_"],
                java_class=(r["text2_"] if (r["var_type_"] or "") == "serializable" else None),
                create_time=r["create_time_"], act_inst_id=r["act_inst_id_"],
                act_id=(act.act_id if act else None),
                scope=_scope_label(r),
                size_bytes=(r["byte_len"] if r["bytearray_id_"] else r["text_len"]),
                in_bytearray=bool(r["bytearray_id_"]), bytearray_id=r["bytearray_id_"],
                revision=r["rev_"], state=r["state_"],
                value_shown=value, value_allowed=allowed, value_reason=reason,
                value_truncated=truncated, value_on_request=on_request))

    children: list[InstanceNode] = []
    cres = _safe(db, _SQL_CHILDREN, {"id": proc_inst_id}, limit=1000, name="instance_children",
                 timeout_ms=timeout_ms)
    if cres is not None:
        children = [_node_from_row(r, key_of_interest=node.business_key) for r in cres.dicts()]

    parent: InstanceNode | None = None
    if node.parent_id:
        pres = _safe(db, _SQL_PARENT, {"id": node.parent_id}, limit=2, name="instance_parent",
                     timeout_ms=timeout_ms)
        if pres is not None and pres.rows:
            parent = _node_from_row(pres.dicts()[0], key_of_interest=node.business_key)
        else:
            notes.append(CaseNote("warn", "The parent process is not (or no longer) present "
                                          "in this history."))

    incidents: list[tuple[str, str, datetime | None]] = []
    ires = _safe(db, _SQL_INCIDENTS, {"id": proc_inst_id}, limit=200, name="instance_incidents",
                 timeout_ms=timeout_ms)
    if ires is not None:
        incidents = [(r["incident_type_"], r["msg"][:300], r["create_time_"]) for r in ires.dicts()]

    bpmn_name, bpmn_bytes = None, None
    bres = _safe(db, _SQL_BPMN_META, {"def_id": node.def_id}, limit=2, name="instance_bpmn_meta",
                 timeout_ms=timeout_ms)
    if bres is not None and bres.rows:
        row = bres.dicts()[0]
        bpmn_name, bpmn_bytes = row["name_"], row["bytes"]

    if node.is_running:
        notes.append(CaseNote("info", "This instance is still running -- the run is incomplete."))
    if node.removal_time:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if node.removal_time < now:
            notes.append(CaseNote("warn", "This instance is past its removal_time_: history "
                                          "cleanup is allowed to delete it."))

    return InstanceDetail(
        instance=node,
        definition_name=head.get("def_name"),
        definition_version=head.get("version"),
        deployment_id=head.get("deployment_id_"),
        bpmn_resource=bpmn_name,
        bpmn_bytes=bpmn_bytes,
        activities=tuple(acts),
        tasks=tuple(tasks),
        variables=tuple(variables),
        children=tuple(children),
        parent=parent,
        open_incidents=tuple(incidents),
        notes=tuple(notes),
        value_policy=access.policy.value,
        value_policy_reason=access.reason,
        loaded_at=datetime.now(timezone.utc),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def load_bpmn_xml(db: Database, def_id: str, *, timeout_ms: int | None = None) -> str | None:
    """The BPMN of the version **this instance actually ran**, not the newest one."""
    res = _safe(db, _SQL_BPMN_XML, {"def_id": def_id}, limit=2, name="instance_bpmn_xml",
                timeout_ms=timeout_ms)
    if res is None or not res.rows:
        return None
    return res.rows[0][0]


def load_large_value(db: Database, profile: Profile, bytearray_id: str, def_key: str | None,
                     name: str, *, limit_bytes: int = REQUEST_MAX_BYTES,
                     timeout_ms: int | None = None) -> tuple[str | None, int | None, str]:
    """Load a bytearray value on explicit request, with an upper bound."""
    access = resolve_access(profile)
    if not access.allows(def_key, name):
        return None, None, access.why_not(def_key, name)
    res = _safe(db, _SQL_ONE_BYTEARRAY, {"id": bytearray_id, "limit": limit_bytes},
                limit=2, name="instance_large_value", timeout_ms=timeout_ms)
    if res is None or not res.rows:
        return None, None, "Value not found."
    row = res.dicts()[0]
    size = row["byte_len"]
    hint = ""
    if size and size > limit_bytes:
        hint = f"Showing the first {limit_bytes:,} of {size:,} bytes."
    return row["text_value"], size, hint


_SQL_ONE_VARIABLE = """
    SELECT v.id_, v.name_, v.var_type_, v.text_, v.text2_, v.long_, v.double_,
           v.bytearray_id_, v.proc_def_key_,
           octet_length(b.bytes_) AS byte_len,
           CASE WHEN v.var_type_ IN ('json', 'xml', 'spin://application/json', 'string')
                     AND octet_length(b.bytes_) <= %(limit)s
                THEN convert_from(substring(b.bytes_ from 1 for %(limit)s), 'UTF8') END AS bytes_text,
           -- For binary values (Java object streams) the escaped byte representation: not
           -- pretty, but better than an empty cell -- a serialised java.time.LocalDate, for
           -- instance, contains the date in readable form. The view labels it as raw bytes.
           CASE WHEN v.bytearray_id_ IS NOT NULL AND octet_length(b.bytes_) <= 8192
                THEN encode(substring(b.bytes_ from 1 for 8192), 'escape') END AS bytes_escape
      FROM act_hi_varinst v
      LEFT JOIN act_ge_bytearray b ON b.id_ = v.bytearray_id_
     WHERE v.proc_inst_id_ = %(pid)s
       AND v.name_ = %(name)s
     ORDER BY v.create_time_
"""


def load_value(db: Database, profile: Profile, proc_inst_id: str, name: str, *,
               limit_bytes: int = REQUEST_MAX_BYTES,
               timeout_ms: int | None = None) -> dict[str, Any]:
    """Fetch ONE variable value in full -- for the value dialog opened by a click.

    The variable table shows only a short preview; the whole value is fetched when somebody
    actually looks at it. That keeps the page small and the database quiet, and it is what makes
    "large and binary values are never loaded automatically" true rather than aspirational.
    """
    access = resolve_access(profile)
    res = _safe(db, _SQL_ONE_VARIABLE, {"pid": proc_inst_id, "name": name, "limit": limit_bytes},
                limit=20, name="instance_single_value", timeout_ms=timeout_ms)
    if res is None or not res.rows:
        return {"allowed": False, "reason": "Variable not found.", "value": None}
    rows = res.dicts()
    row = rows[-1]                       # the most recent entry for this name
    def_key = row.get("proc_def_key_")
    if not access.allows(def_key, name):
        return {"allowed": False, "reason": access.why_not(def_key, name), "value": None,
                "type": row.get("var_type_"), "size": row.get("byte_len")}

    var_type = (row.get("var_type_") or "").lower()
    hint = ""
    if row.get("bytearray_id_"):
        value = row.get("bytes_text")
        size = row.get("byte_len")
        if value is None:
            hint = ("Binary value (a Java object stream or raw bytes) -- not resolvable as text "
                    "without the application's own classes. The raw bytes are shown below.")
        elif size and size > limit_bytes:
            hint = f"Showing the first {limit_bytes:,} of {size:,} bytes."
    else:
        value, _allowed, reason, _truncated, _on_request = _render_value(
            {**row, "bytes_text": None, "text_len": None}, access, def_key)
        if value is None and reason:
            hint = reason
        size = len(row.get("text_") or "") if row.get("text_") is not None else None

    return {
        "allowed": True, "reason": "", "value": value, "raw_bytes": row.get("bytes_escape"),
        "type": row.get("var_type_"),
        "size": size, "java_class": (row.get("text2_") if var_type == "serializable" else None),
        "hint": hint, "occurrences": len(rows),
        "formattable": var_type in ("json", "xml", "spin://application/json"),
    }
