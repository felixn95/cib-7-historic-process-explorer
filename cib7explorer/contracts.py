"""Shared data types.

This module imports nothing from the web layer and nothing from psycopg. It is the contract
between data access (``cib7explorer.db``), restore (``cib7explorer.restore``) and the web
interface (``cib7explorer.web``) -- which keeps the query logic callable and testable without
starting a server.

One theme runs through all of these types: **a missing number is not a zero.** Several of
them therefore carry ``None`` where other designs would put ``0``, together with a field or
property explaining why the number is missing. Engine history is only as complete as the
configured history level allows, and a report that silently prints ``0`` for "not recorded"
is worse than one that says nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Classification(str, Enum):
    """How sensitive the data behind a connection profile is.

    Drives two things: whether variable values may be rendered by default, and a permanent
    marker in the page header so nobody forgets which database they are looking at.
    """

    TEST = "test"
    UNKNOWN = "unknown"
    PROD = "prod"

    @property
    def values_mode_default(self) -> bool:
        return self is Classification.TEST


class ProfileKind(str, Enum):
    LOCAL_RESTORE = "local_restore"   # dump file the tool restores into its own container
    DIRECT = "direct"                 # database that is already reachable
    SSH_TUNNEL = "ssh_tunnel"         # reserved; not implemented


class HistoryLevel(int, Enum):
    NONE = 0
    ACTIVITY = 1
    AUDIT = 2
    FULL = 3

    @property
    def label(self) -> str:
        return self.name

    @classmethod
    def parse(cls, raw: str | int | None) -> "HistoryLevel | None":
        if raw is None:
            return None
        try:
            return cls(int(raw))
        except (TypeError, ValueError):
            return None


class Feature(str, Enum):
    """Capabilities that depend on the history level and on what the data actually contains.

    The mapping from feature to table is checked at connect time rather than assumed. An empty
    table means the numbers that depend on it are reported as "not recorded" instead of zero.
    """

    PROCESS_INSTANCES = "process_instances"
    ACTIVITY_INSTANCES = "activity_instances"
    TASK_INSTANCES = "task_instances"
    VARIABLE_INSTANCES = "variable_instances"
    VARIABLE_UPDATES = "variable_updates"          # act_hi_detail, FULL only
    HISTORIC_INCIDENTS = "historic_incidents"      # act_hi_incident, FULL only
    OPEN_INCIDENTS = "open_incidents"              # act_ru_incident, independent of the level
    IDENTITY_LINKS = "identity_links"              # act_hi_identitylink, FULL only
    OPERATION_LOG = "operation_log"                # act_hi_op_log, FULL only
    JOB_LOG = "job_log"                            # act_hi_job_log, FULL only
    EXTERNAL_TASK_LOG = "external_task_log"        # act_hi_ext_task_log, FULL only
    DECISION_INSTANCES = "decision_instances"      # act_hi_decinst, FULL only
    BPMN_RESOURCES = "bpmn_resources"              # act_ge_bytearray type_=1


@dataclass(frozen=True)
class FeatureStatus:
    """*Why* a capability is missing matters more than the fact that it is missing."""

    feature: Feature
    available: bool
    table: str
    table_exists: bool
    has_rows: bool
    est_rows: int | None
    reason: str = ""


@dataclass(frozen=True)
class TableInfo:
    name: str
    exists: bool
    est_rows: int | None          # estimate from pg_class.reltuples -- never count(*)
    total_bytes: int | None
    has_rows: bool | None         # cheap EXISTS probe, not a count


@dataclass(frozen=True)
class SchemaDeviation:
    table: str
    kind: str                     # 'missing_table' | 'missing_column' | 'extra_column' | 'type_changed'
    detail: str


@dataclass(frozen=True)
class HistoryWindow:
    """The period the history covers, plus everything that trims it."""

    first_start: datetime | None
    last_start: datetime | None
    last_end: datetime | None
    running_instances: int | None
    removal_time_min: datetime | None
    removal_time_max: datetime | None
    rows_past_removal_time: int | None      # > 0 => history cleanup has not caught up yet
    instances_without_removal_time: int | None


@dataclass(frozen=True)
class TimezoneEvidence:
    """The engine writes naive timestamps in the zone of its own JVM.

    That zone is not stored anywhere in the database, so it is evidenced rather than claimed:
    server time compared against the most recent timestamp, plus the hour-of-day distribution
    of instance starts. Both are shown to the reader, who can then judge the configured value.
    """

    db_now: datetime | None
    db_timezone_setting: str | None
    latest_history_timestamp: datetime | None
    lag_to_db_now_seconds: float | None
    start_hour_histogram: dict[int, int] = field(default_factory=dict)
    configured_source_timezone: str = "UTC"
    configured_display_timezone: str = "Europe/Berlin"


@dataclass(frozen=True)
class DetectionResult:
    """Everything established while connecting, so the interface can state what it is reading."""

    profile_name: str
    classification: Classification
    server_version: str
    database_name: str
    connected_as: str
    session_is_read_only: bool
    installation_id: str | None
    engine_schema_version: str | None          # act_ge_schema_log, highest version
    schema_log: list[tuple[str, datetime | None]] = field(default_factory=list)
    flyway_migrations: list[tuple[str, str, datetime | None]] = field(default_factory=list)
    history_level: HistoryLevel | None = None
    history_level_raw: str | None = None
    history_window: HistoryWindow | None = None
    tables: list[TableInfo] = field(default_factory=list)
    features: list[FeatureStatus] = field(default_factory=list)
    deviations: list[SchemaDeviation] = field(default_factory=list)
    tenant_ids: list[str | None] = field(default_factory=list)
    timezone: TimezoneEvidence | None = None
    detected_at: datetime | None = None
    duration_ms: int | None = None

    def feature(self, f: Feature) -> FeatureStatus | None:
        for s in self.features:
            if s.feature is f:
                return s
        return None

    def has(self, f: Feature) -> bool:
        s = self.feature(f)
        return bool(s and s.available)


@dataclass(frozen=True)
class QueryResult:
    """Every query reports whether it hit a limit."""

    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    limit: int | None
    duration_ms: int
    statement_timeout_ms: int

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r)) for r in self.rows]

    @property
    def one(self) -> dict[str, Any] | None:
        d = self.dicts()
        return d[0] if d else None


class RestorePhase(str, Enum):
    ABSENT = "absent"
    CHECKING = "checking"
    CREATING_CONTAINER = "creating_container"
    RESTORING = "restoring"
    POST_PROCESSING = "post_processing"   # ANALYZE, read-only role, container back to normal
    READY = "ready"
    FAILED = "failed"


@dataclass
class RestoreState:
    """Progress of a dump restore.

    Kept as JSON in the state directory so an interrupted run is recognisable and repeatable
    instead of leaving a half-filled database that looks ready.
    """

    profile_name: str
    dump_path: str
    dump_size_bytes: int
    dump_fingerprint: str
    phase: RestorePhase = RestorePhase.ABSENT
    message: str = ""
    toc_items_total: int | None = None
    toc_items_done: int = 0
    tables_done: list[str] = field(default_factory=list)
    current_item: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    adopted_existing: bool = False
    source_server_version: str | None = None
    log_tail: list[str] = field(default_factory=list)

    @property
    def percent(self) -> int | None:
        if not self.toc_items_total:
            return None
        return min(100, int(100 * self.toc_items_done / self.toc_items_total))

    @property
    def is_terminal(self) -> bool:
        return self.phase in (RestorePhase.READY, RestorePhase.FAILED)


# =========================================================================================
# Process definitions and their variable catalogue
# =========================================================================================


@dataclass(frozen=True)
class DurationStats:
    """A distribution, not an average.

    Process runtimes are heavily skewed -- a handful of instances that waited on a human for
    three weeks pull the mean far away from anything typical. Percentiles say what actually
    happened. All values in milliseconds; ``None`` where there is no data to base them on.
    """

    n: int = 0
    n_unfinished: int = 0        # running or terminated instances without duration_
    p25: int | None = None
    p50: int | None = None
    p75: int | None = None
    p90: int | None = None
    p99: int | None = None
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class SizeStats:
    """Size distribution of a variable's payload -- measured *without* reading the payload."""

    n: int = 0
    minimum: int | None = None
    p50: int | None = None
    p90: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class EndActivity:
    act_id: str | None
    instances: int
    validation_only: bool = False      # matched against the profile's pattern list
    validation_related: bool = False


@dataclass(frozen=True)
class DefinitionSummary:
    """One process definition as it appears in the history."""

    key: str
    name: str | None = None
    deployed: bool = True                     # does the key still appear in act_re_procdef
    deployed_versions: int | None = None
    latest_deployed_version: int | None = None
    suspended_versions: int | None = None
    versions_used: int = 0                    # versions with at least one instance

    instances: int = 0
    instances_as_root: int = 0
    instances_as_child: int = 0

    completed: int = 0
    externally_terminated: int = 0
    internally_terminated: int = 0
    active: int = 0
    state_other: int = 0

    first_start: datetime | None = None
    last_start: datetime | None = None
    last_end: datetime | None = None

    distinct_business_keys: int = 0
    instances_without_business_key: int = 0

    duration: DurationStats = field(default_factory=DurationStats)

    #: Open incidents from act_ru_incident. ``None`` means "cannot be determined", ``0`` means
    #: "none open" -- which is NOT the same as "no incidents in the history", because
    #: act_hi_incident is only written at history level FULL.
    open_incidents: int | None = None
    historic_incidents: int | None = None

    user_task_instances: int | None = None
    distinct_assignees: int | None = None

    end_activities: tuple[EndActivity, ...] = ()

    #: Instances whose ``onlyValidation`` process variable is true -- the technical marker for
    #: a dry run that validates input without doing the work. ``None`` means "not determined",
    #: ``0`` means "none". Installations that use this pattern can have a large share of such
    #: instances, and for some definitions nearly all of them; without this number the plain
    #: instance count is misleading.
    #:
    #: Only the value present *at instance start* counts, i.e. the input parameter. Some
    #: processes set the same variable internally later on, where it means something else. The
    #: variable is written as a boolean by some processes and as a string by others.
    validation_flag_true: int | None = None
    validation_flag_false: int | None = None
    validation_flag_undecidable: int | None = None
    #: Instances carrying the marker only internally, never as an input parameter -- for those
    #: the invocation mode cannot be determined from the data, and it is not guessed.
    validation_flag_not_at_start: int | None = None

    @property
    def validation_flag_share(self) -> float | None:
        if not self.instances or self.validation_flag_true is None:
            return None
        return self.validation_flag_true / self.instances

    @property
    def validation_only_instances(self) -> int:
        return sum(e.instances for e in self.end_activities if e.validation_only)

    @property
    def validation_related_instances(self) -> int:
        return sum(e.instances for e in self.end_activities if e.validation_related)

    @property
    def only_as_child(self) -> bool:
        return self.instances > 0 and self.instances_as_root == 0

    @property
    def only_as_root(self) -> bool:
        return self.instances > 0 and self.instances_as_child == 0

    @property
    def both_roles(self) -> bool:
        return self.instances_as_root > 0 and self.instances_as_child > 0


@dataclass(frozen=True)
class DefinitionVersionRow:
    """Instances per definition version.

    Installations that redeploy on every build accumulate hundreds of versions per key, which
    is why this breakdown is loaded on demand rather than being part of the list.
    """

    key: str
    proc_def_id: str
    version: int | None
    instances: int
    first_start: datetime | None
    last_start: datetime | None
    deployed_at: datetime | None = None
    version_tag: str | None = None


@dataclass(frozen=True)
class FirstWriteActivity:
    act_id: str
    act_type: str | None
    occurrences: int


@dataclass(frozen=True)
class SerializationForm:
    var_type: str | None
    java_class: str | None          # act_hi_varinst.text2_ for serializable values
    occurrences: int

    @property
    def resolvable_without_application(self) -> bool | None:
        """Whether the value could be read without the application's own classes.

        json/xml/primitive: yes. serializable holding a JDK class: yes. serializable holding an
        application class: no. Anything unknown stays ``None`` rather than becoming a claim.
        """
        if self.var_type is None:
            return None
        if self.var_type == "serializable":
            if not self.java_class:
                return None
            return self.java_class.startswith(("java.", "javax.", "jdk."))
        return True


@dataclass(frozen=True)
class VariableCatalogEntry:
    """A variable as it occurs in a process definition -- names and shapes, never values.

    This structure must never carry a variable value. It is what an allowlist is written
    against, and it has to be shareable with people who are not allowed to see values.
    """

    def_key: str
    name: str

    types: tuple[str, ...] = ()
    type_switch: bool = False              # more than one technical type -> name collision?

    occurrences: int = 0                   # variable instances
    instances_with: int = 0                # distinct process instances
    def_instances: int = 0                 # denominator: instances of this definition
    null_typed: int = 0                    # var_type_ = 'null': present, but without a value
    #: Process instances holding more than one variable instance of the same name. That is NOT
    #: overwriting (which would live in act_hi_detail, empty at AUDIT) but multiple scopes --
    #: typically multi-instance loops or subprocesses.
    instances_multi_scope: int = 0

    first_write_at_instance_level: int = 0
    first_write_activities: tuple[FirstWriteActivity, ...] = ()
    from_call_activity: int = 0            # first written at a call activity

    scope_process_instance: int = 0
    scope_below_process_instance: int = 0
    scope_task_local: int = 0

    serialization: tuple[SerializationForm, ...] = ()
    inline_size: SizeStats = field(default_factory=SizeStats)
    bytearray_size: SizeStats = field(default_factory=SizeStats)

    first_seen: datetime | None = None
    last_seen: datetime | None = None
    version_min: int | None = None
    version_max: int | None = None
    in_latest_used_version: bool | None = None

    @property
    def share_of_instances(self) -> float | None:
        if not self.def_instances:
            return None
        return self.instances_with / self.def_instances

    @property
    def share_with_value(self) -> float | None:
        """"Occurs" and "has a value" are two different numbers.

        A variable instance of type ``null`` exists and carries nothing, and there are usually
        many of them -- counting them as present values overstates what the data holds.
        """
        if not self.def_instances or not self.occurrences:
            return None
        return (self.occurrences - self.null_typed) / self.occurrences

    @property
    def resolvability(self) -> bool | None:
        vals = [s.resolvable_without_application for s in self.serialization]
        if not vals or all(v is None for v in vals):
            return None
        if all(v for v in vals if v is not None):
            return True
        return False


@dataclass(frozen=True)
class CrossProcessVariable:
    """A variable name occurring in more than one process definition.

    A candidate for a shared object reference -- explicitly only a candidate. Whether two
    identically named variables mean the same thing is not something the data can say.
    """

    name: str
    def_count: int
    definitions: tuple[str, ...]
    types: tuple[str, ...]
    type_conflict: bool                # same name, different types across definitions
    occurrences: int
    instances_with: int


@dataclass(frozen=True)
class CatalogMeta:
    """When a precomputation was made -- has to be visible in the view that uses it."""

    built_at: datetime | None = None
    duration_ms: int | None = None
    profile_name: str = ""
    installation_id: str | None = None
    history_level: str | None = None
    rows: int = 0
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class VariableCatalog:
    entries: tuple[VariableCatalogEntry, ...] = ()
    cross_process: tuple[CrossProcessVariable, ...] = ()
    meta: CatalogMeta = field(default_factory=CatalogMeta)


# =========================================================================================
# The case: everything that belongs to one business key
# =========================================================================================


class InstanceOrigin(str, Enum):
    """How an instance belongs to a case.

    Instances started through a call activity inherit the business key only when the model says
    so. There are therefore instances that belong to a case in every meaningful sense while
    carrying no key at all -- and instances carrying a *different* key, which is itself
    significant (an offer process starting a policy process). Both have to stay visibly
    distinct instead of being blended into one list.
    """

    OWN_KEY = "own_key"          # carries the key being searched for
    NO_KEY = "no_key"            # carries no key, belongs via the parent chain
    OTHER_KEY = "other_key"      # carries a different key -- a key change

    @property
    def label(self) -> str:
        return {
            InstanceOrigin.OWN_KEY: "carries the key",
            InstanceOrigin.NO_KEY: "no key of its own",
            InstanceOrigin.OTHER_KEY: "different key",
        }[self]


class StartTrigger(str, Enum):
    """What started an instance -- as far as the data says.

    ``start_user_id_`` is frequently NULL for every instance in an installation, so "started by
    a user" is usually not evidenced and is therefore not claimed. When nothing remains, the
    answer is ``UNKNOWN`` rather than a plausible guess such as "API".
    """

    PARENT_PROCESS = "parent_process"
    SIGNAL = "signal"
    MESSAGE = "message"
    TIMER = "timer"
    CONDITIONAL = "conditional"
    PLAIN_START = "plain_start"       # noneStartEvent: triggered from outside (API/message)
    USER = "user"
    RESTART = "restart"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            StartTrigger.PARENT_PROCESS: "parent process",
            StartTrigger.SIGNAL: "signal",
            StartTrigger.MESSAGE: "message",
            StartTrigger.TIMER: "timer",
            StartTrigger.CONDITIONAL: "condition",
            StartTrigger.PLAIN_START: "started from outside (API)",
            StartTrigger.USER: "user",
            StartTrigger.RESTART: "restart of an instance",
            StartTrigger.UNKNOWN: "not derivable from the data",
        }[self]


@dataclass(frozen=True)
class InstanceNode:
    """One process instance within a case."""

    proc_inst_id: str
    def_key: str
    def_id: str
    parent_id: str | None = None
    root_id: str | None = None
    version: int | None = None
    business_key: str | None = None
    origin: InstanceOrigin = InstanceOrigin.OWN_KEY

    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_ms: int | None = None
    state: str | None = None
    delete_reason: str | None = None
    start_act_id: str | None = None
    end_act_id: str | None = None
    removal_time: datetime | None = None
    restarted_from: str | None = None

    start_trigger: StartTrigger = StartTrigger.UNKNOWN
    start_trigger_detail: str | None = None

    depth: int = 0
    child_ids: tuple[str, ...] = ()

    #: The ``onlyValidation`` input parameter. ``None`` means "cannot be determined" -- not "no".
    validation_only: bool | None = None
    open_incidents: int = 0
    user_task_count: int | None = None

    #: Two instances with an identical start timestamp: their order is undetermined and is
    #: marked as such instead of appearing arbitrarily sorted. ``act_hi_procinst`` has no
    #: sequence counter, so there is nothing that could decide the order.
    order_ambiguous: bool = False
    orphaned_parent: bool = False

    @property
    def is_running(self) -> bool:
        return self.end_time is None

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def terminated(self) -> bool:
        return bool(self.state and self.state.endswith("TERMINATED"))


class GapKind(str, Enum):
    BETWEEN = "between"          # a real gap between two root instances
    OVERLAP = "overlap"          # a negative "gap": the trees ran in parallel
    BEFORE_WINDOW = "before"     # the case starts at the edge of the retained history
    OPEN_END = "open_end"        # something is still running


@dataclass(frozen=True)
class Gap:
    """A period during which no process ran for this case.

    Gaps are a data type of their own rather than a by-product of rendering, because they are
    often the most informative part of a case: they are where the work sat waiting for
    somebody, and they are invisible in any per-instance view.
    """

    kind: GapKind
    start: datetime | None
    end: datetime | None
    duration_ms: int | None
    after_root: str | None = None
    before_root: str | None = None
    after_def: str | None = None
    before_def: str | None = None


@dataclass(frozen=True)
class TreeSummary:
    """A call tree: one root instance plus everything it started.

    The tree, not the instance, is the unit of the timeline. A busy case can hold twice as many
    instances as trees, and a timeline drawn per instance stops being readable long before a
    timeline drawn per tree does.
    """

    root_id: str
    def_key: str
    start_time: datetime | None
    end_time: datetime | None
    instance_count: int
    max_depth: int
    instance_ids: tuple[str, ...] = ()
    business_keys: tuple[str, ...] = ()
    validation_only: bool | None = None
    open_incidents: int = 0
    running: bool = False
    terminated: bool = False

    @property
    def duration_ms(self) -> int | None:
        if not self.start_time or not self.end_time:
            return None
        return int((self.end_time - self.start_time).total_seconds() * 1000)


@dataclass(frozen=True)
class ForeignKeyLink:
    """A different business key showing up inside this case.

    Deliberately NOT merged in: such a key usually carries further instances belonging to a
    different chain, and pulling them in would silently widen the case into something the user
    never asked for. So: a count and a link to jump, no quiet expansion.
    """

    key: str
    instances_in_case: int
    instances_total: int | None
    first_seen: datetime | None = None
    via_definitions: tuple[str, ...] = ()

    @property
    def instances_outside(self) -> int | None:
        if self.instances_total is None:
            return None
        return max(0, self.instances_total - self.instances_in_case)


@dataclass(frozen=True)
class Correlation:
    """Second track: instances carrying the same value of an object-id variable.

    A plain count ("these 36 instances carry ticketNumber = X"), never a claim that they mean
    the same thing. Never merged into the case itself.
    """

    variable: str
    value_shown: str
    instances_in_case: int
    instances_total: int
    outside_ids: tuple[str, ...] = ()
    outside_def_keys: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def instances_outside(self) -> int:
        return max(0, self.instances_total - self.instances_in_case)


@dataclass(frozen=True)
class CaseNote:
    """A caveat that belongs on the page itself, not in documentation nobody opens."""

    level: str        # 'info' | 'warn'
    text: str


@dataclass(frozen=True)
class Case:
    key: str
    instances: tuple[InstanceNode, ...] = ()
    trees: tuple[TreeSummary, ...] = ()
    gaps: tuple[Gap, ...] = ()
    foreign_keys: tuple[ForeignKeyLink, ...] = ()
    notes: tuple[CaseNote, ...] = ()

    window_start: datetime | None = None
    window_end: datetime | None = None
    history_first_start: datetime | None = None
    truncated_left: bool = False
    open_end: bool = False
    partially_removable: bool = False

    instances_shown: int = 0
    instances_total: int = 0        # present in the closure, even when not all are shown
    trees_total: int = 0
    loaded_at: datetime | None = None
    duration_ms: int | None = None

    @property
    def instances_with_own_key(self) -> int:
        return sum(1 for i in self.instances if i.origin is InstanceOrigin.OWN_KEY)

    @property
    def instances_without_key(self) -> int:
        return sum(1 for i in self.instances if i.origin is InstanceOrigin.NO_KEY)

    @property
    def instances_with_other_key(self) -> int:
        return sum(1 for i in self.instances if i.origin is InstanceOrigin.OTHER_KEY)

    @property
    def definitions(self) -> tuple[str, ...]:
        return tuple(sorted({i.def_key for i in self.instances}))

    @property
    def validation_only_count(self) -> int:
        return sum(1 for i in self.instances if i.validation_only)

    @property
    def running_count(self) -> int:
        return sum(1 for i in self.instances if i.is_running)

    @property
    def total_span_ms(self) -> int | None:
        if not self.window_start or not self.window_end:
            return None
        return int((self.window_end - self.window_start).total_seconds() * 1000)


@dataclass(frozen=True)
class BusinessKeyHit:
    """One search hit."""

    key: str
    instances: int
    definitions: int
    first_start: datetime | None
    last_activity: datetime | None


@dataclass(frozen=True)
class BusinessKeySummary:
    """One row of the browse list, so that interesting cases can be found without knowing a
    single key up front."""

    key: str
    instances: int
    root_instances: int
    definitions: int
    first_start: datetime | None
    last_activity: datetime | None
    running: int = 0
    terminated: int = 0
    open_incidents: int = 0
    largest_gap_ms: int | None = None
    span_ms: int | None = None
    validation_only: int | None = None


# =========================================================================================
# Zooming into a single process instance
# =========================================================================================


@dataclass(frozen=True)
class ActivityRun:
    """One executed activity."""

    act_inst_id: str
    act_id: str
    act_type: str
    act_name: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_ms: int | None = None
    sequence_counter: int | None = None
    parent_act_inst_id: str | None = None
    task_id: str | None = None
    call_proc_inst_id: str | None = None
    assignee: str | None = None
    act_inst_state: int | None = None
    order_ambiguous: bool = False

    @property
    def is_open(self) -> bool:
        return self.end_time is None


@dataclass(frozen=True)
class TaskRun:
    """A user task, as far as the history goes.

    At history level AUDIT, ``act_hi_taskinst`` holds only creation, end, assignee and
    duration. The life cycle -- when it was claimed, delegated, reassigned -- and with it any
    split between waiting time and working time would live in ``act_hi_op_log`` and
    ``act_hi_identitylink``, both of which are empty at AUDIT. That absence is displayed, not
    estimated.
    """

    task_id: str
    name: str | None = None
    task_def_key: str | None = None
    assignee: str | None = None
    owner: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_ms: int | None = None
    priority: int | None = None
    due_date: datetime | None = None
    follow_up_date: datetime | None = None
    delete_reason: str | None = None
    task_state: str | None = None
    act_inst_id: str | None = None
    claim_time_available: bool = False
    lifecycle_note: str = ""


@dataclass(frozen=True)
class VariableEntry:
    """A variable of an instance. The value appears only when the value policy allows it."""

    name: str
    var_type: str | None
    java_class: str | None = None
    create_time: datetime | None = None
    act_inst_id: str | None = None
    act_id: str | None = None
    scope: str = "process instance"
    size_bytes: int | None = None
    in_bytearray: bool = False
    bytearray_id: str | None = None
    revision: int | None = None
    state: str | None = None

    value_shown: str | None = None
    value_allowed: bool = False
    value_reason: str = ""
    value_truncated: bool = False
    value_on_request: bool = False        # too large or binary: only on explicit request

    @property
    def has_value_in_db(self) -> bool:
        return self.var_type not in (None, "null")


@dataclass(frozen=True)
class InstanceDetail:
    """Everything about one process instance."""

    instance: InstanceNode
    definition_name: str | None = None
    definition_version: int | None = None
    deployment_id: str | None = None
    bpmn_resource: str | None = None
    bpmn_bytes: int | None = None

    activities: tuple[ActivityRun, ...] = ()
    tasks: tuple[TaskRun, ...] = ()
    variables: tuple[VariableEntry, ...] = ()
    children: tuple[InstanceNode, ...] = ()
    parent: InstanceNode | None = None
    open_incidents: tuple[tuple[str, str, datetime | None], ...] = ()
    notes: tuple[CaseNote, ...] = ()

    value_policy: str = "none"
    value_policy_reason: str = ""
    loaded_at: datetime | None = None
    duration_ms: int | None = None

    @property
    def visited_act_ids(self) -> tuple[str, ...]:
        return tuple({a.act_id for a in self.activities})

    @property
    def open_act_ids(self) -> tuple[str, ...]:
        return tuple({a.act_id for a in self.activities if a.is_open})


# =========================================================================================
# The process landscape in numbers
# =========================================================================================
#
# Everything below is **plain counting**: frequencies, distributions, time series. No scoring,
# no thresholds, no "anomaly" flags, and no averaged indicator that hides the distribution it
# came from. Whether a number is good or bad is a domain question, and this tool does not know
# the domain.


@dataclass(frozen=True)
class MonthCount:
    def_key: str
    month: datetime
    instances: int
    versions: int


@dataclass(frozen=True)
class CallEdge:
    """A call relationship between two process definitions, aggregated over all instances."""

    parent_def: str
    child_def: str
    calls: int
    parent_instances: int

    @property
    def calls_per_parent(self) -> float | None:
        return self.calls / self.parent_instances if self.parent_instances else None


@dataclass(frozen=True)
class CoOccurrence:
    """Two definitions appearing on the same business key.

    Explicitly different from the call graph: neither starts the other. This is the layer of
    process structure that nobody modelled -- it exists only in the data.
    """

    def_a: str
    def_b: str
    keys: int
    also_calls: bool = False       # the two are additionally connected by a call edge


@dataclass(frozen=True)
class Transition:
    """An observed transition: which definition follows which, when the root instances of a
    business key are put in chronological order.

    **This is not a process model.** No model exists at this level -- the count says only how
    often an order was observed. Rare transitions are not filtered away, because a transition
    seen twice can matter more than one seen ten thousand times.
    """

    from_def: str
    to_def: str
    count: int
    keys: int
    median_gap_ms: int | None = None


@dataclass(frozen=True)
class SequencePattern:
    """An observed sequence of process definitions across a whole case."""

    sequence: tuple[str, ...]
    count: int
    example_keys: tuple[str, ...] = ()

    @property
    def length(self) -> int:
        return len(self.sequence)


@dataclass(frozen=True)
class Distribution:
    """A distribution -- median and quartiles instead of a mean."""

    n: int = 0
    minimum: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None
    p99: float | None = None
    maximum: float | None = None

    @classmethod
    def from_values(cls, values: list[float]) -> "Distribution":
        if not values:
            return cls()
        s = sorted(values)
        def q(p: float) -> float:
            if len(s) == 1:
                return s[0]
            idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
            return s[idx]
        return cls(n=len(s), minimum=s[0], p25=q(0.25), p50=q(0.5), p75=q(0.75),
                   p90=q(0.9), p99=q(0.99), maximum=s[-1])


@dataclass(frozen=True)
class ActorStats:
    distinct_assignees: int = 0
    assigned_tasks: int = 0
    unassigned_tasks: int = 0
    per_assignee: tuple[tuple[str, int], ...] = ()
    definitions_with_human_tasks: int = 0
    definitions_fully_automated: int = 0
    start_users_available: bool = False


@dataclass(frozen=True)
class DisruptionStats:
    """Disruptions, counted -- as frequencies, not as a quality verdict."""

    per_definition: tuple[tuple[str, int, int, int, int | None], ...] = ()
    # (def_key, instances, externally terminated, internally terminated, open incidents)
    historic_incidents_available: bool = False
    operation_log_available: bool = False


@dataclass(frozen=True)
class LandscapeMeta:
    built_at: datetime | None = None
    duration_ms: int | None = None
    profile_name: str = ""
    window_start: datetime | None = None
    window_end: datetime | None = None
    instances_total: int = 0
    root_instances: int = 0
    keys_total: int = 0
    definitions_total: int = 0
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Landscape:
    monthly: tuple[MonthCount, ...] = ()
    call_edges: tuple[CallEdge, ...] = ()
    depth_distribution: tuple[tuple[int, int], ...] = ()
    only_root: tuple[str, ...] = ()
    only_child: tuple[str, ...] = ()
    both_roles: tuple[str, ...] = ()
    co_occurrence: tuple[CoOccurrence, ...] = ()
    transitions: tuple[Transition, ...] = ()
    entry_defs: tuple[tuple[str, int], ...] = ()
    exit_defs: tuple[tuple[str, int], ...] = ()
    sequences: tuple[SequencePattern, ...] = ()
    sequences_distinct: int = 0
    sequences_unique_once: int = 0
    instances_per_key: Distribution = field(default_factory=Distribution)
    definitions_per_key: Distribution = field(default_factory=Distribution)
    span_per_key_ms: Distribution = field(default_factory=Distribution)
    gaps_ms: Distribution = field(default_factory=Distribution)
    gap_counts: tuple[tuple[str, int], ...] = ()
    overlap_pairs: int = 0
    gap_pairs: int = 0
    actors: ActorStats = field(default_factory=ActorStats)
    disruptions: DisruptionStats = field(default_factory=DisruptionStats)
    validation_only_instances: int | None = None
    meta: LandscapeMeta = field(default_factory=LandscapeMeta)

    @property
    def sequence_concentration(self) -> float | None:
        """Share of cases covered by the ten most frequent sequences.

        A single number that says a great deal about a domain: high concentration means a
        handful of paths carry the business, low concentration means the real process lives in
        the long tail and no diagram will summarise it.
        """
        total = sum(s.count for s in self.sequences)
        if not total:
            return None
        return sum(s.count for s in self.sequences[:10]) / total
