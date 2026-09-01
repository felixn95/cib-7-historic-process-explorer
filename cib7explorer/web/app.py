"""Entry point of the interface: connect, and state honestly what is being read.

This module holds only the pages that come before the views: profile overview, connection,
detection, restore progress and a health endpoint. The four views and the mark list hang off it
as routers of their own (see below), which keeps this file small.

Framework choice: FastAPI + Jinja2, rendered server-side, with HTMX for exactly two things
(loading fragments and posting a form without a page change). The goal is few dependencies and
code that is still maintainable in two years.

Every endpoint is ``def``, not ``async def``: the database layer (``psycopg``) blocks, and
FastAPI runs synchronous endpoints in a thread pool automatically. An ``async def`` endpoint that
then waits synchronously on psycopg would block the event loop -- that is the real risk here, not
thread pool overhead.

The heavier modules (``db.detect``, ``restore.docker_restore``, ``cache.Cache``) are imported
lazily throughout: the interface has to start and stay usable even when one of them cannot be
loaded, because Docker is absent or a dependency is broken. Every such failure turns into an
explanatory box on the page, never into a crashing route.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .. import config
from ..config import Profile, ProfileKind
from ..contracts import (
    Classification,
    DetectionResult,
    Feature,
    FeatureStatus,
    HistoryLevel,
    HistoryWindow,
    RestorePhase,
    SchemaDeviation,
    TableInfo,
    TimezoneEvidence,
)
from ..db import connection
from . import deps

log = logging.getLogger("cib7explorer.web")

_HERE = Path(__file__).parent

app = FastAPI(title="CIB7 Process Explorer")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

templates = Jinja2Templates(directory=str(_HERE / "templates"))

# The views live in routers of their own so that this file does not keep growing.
from .views_definitions import router as _views_definitions_router  # noqa: E402
app.include_router(_views_definitions_router)
from .views_cases import router as _views_cases_router  # noqa: E402
app.include_router(_views_cases_router)
from .views_instance import router as _views_instance_router  # noqa: E402
app.include_router(_views_instance_router)
from .views_landscape import router as _views_landscape_router  # noqa: E402
app.include_router(_views_landscape_router)
from .views_marks import router as _views_marks_router  # noqa: E402
app.include_router(_views_marks_router)
templates.env.filters["num"] = deps.fmt_int
templates.env.filters["bytes_size"] = deps.fmt_bytes
templates.env.filters["datetime"] = deps.fmt_dt
templates.env.filters["ago"] = deps.fmt_ago
templates.env.globals["static_version"] = deps.static_version
templates.env.globals["base_path"] = deps.base_path
templates.env.globals["auth_enabled"] = deps.auth_enabled

# A login, when the environment asks for one (CIB7_OIDC_ISSUER). Without it, everything runs as
# before without a login -- right for a development machine, wrong for a server. A configuration
# error must NOT quietly become "no login": the interface would then stand open and nobody would
# have noticed.
_oidc = config.oidc_from_env()
if _oidc is not None:
    # Under uvicorn the root logger has no handler -- uvicorn configures only its own loggers.
    # Without these two lines every INFO message of this application disappears, including the one
    # that evidences the login mode ("login active: ... client ..."). That is precisely the line
    # somebody wants to read in a container log.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from . import auth as _auth

    _auth.install(app, _oidc)

#: Readable label per capability. The "not recorded" list is the core of what makes this tool
#: trustworthy, so it has to read as a sentence rather than as a raw enum value.
FEATURE_LABELS: dict[Feature, str] = {
    Feature.PROCESS_INSTANCES: "process instances",
    Feature.ACTIVITY_INSTANCES: "activity instances",
    Feature.TASK_INSTANCES: "user tasks",
    Feature.VARIABLE_INSTANCES: "process variables (current values)",
    Feature.VARIABLE_UPDATES: "variable change history",
    Feature.HISTORIC_INCIDENTS: "historic incidents",
    Feature.OPEN_INCIDENTS: "open incidents",
    Feature.IDENTITY_LINKS: "identity links (candidates/assignments, historic)",
    Feature.OPERATION_LOG: "operation log (manual interventions)",
    Feature.JOB_LOG: "job log",
    Feature.EXTERNAL_TASK_LOG: "external task log",
    Feature.DECISION_INSTANCES: "decision instances (DMN)",
    Feature.BPMN_RESOURCES: "BPMN resources (diagrams)",
}


# --- small data types local to this module ------------------------------------------------
#
# ReadOnlyProof (from db.connection) is only meaningful while a connection is open -- with
# connect/close per request (see deps.py) its content would be gone once the connection closes.
# So a plain value copy is kept here, which can be cached alongside the DetectionResult
# (Cache.put expects a serialisable object, not an open database connection).

@dataclass(frozen=True)
class ProofInfo:
    ok: bool
    privileges_clean: bool
    summary: str
    probed_table: str


@dataclass(frozen=True)
class DetectionBundle:
    """What remains of a connection attempt in durable, cacheable form."""

    det: DetectionResult
    proof: ProofInfo
    computed_at: datetime


_CACHE_KEY = "detection"

#: ``Cache.for_detection(profile_name, det)`` needs an *existing* DetectionResult to compute its
#: schema fingerprint, and therefore the file name of the SQLite cache -- but a pure read *before*
#: the first detection does not have that ``det`` yet. A chicken-and-egg problem in the cache API.
#:
#: So this process remembers the most recently resolved cache handle per profile in memory: the
#: first detection after a server start always runs live, and every later render within the same
#: running instance can reuse the known handle for a real read. After a restart it begins fresh,
#: which is more honest than a cache that might hide a by-now-different installation behind the
#: same profile name.
_cache_handles: dict[str, Any] = {}


def _cache_class() -> Any:
    try:
        from ..cache import Cache  # type: ignore[import-not-found]
        return Cache
    except ImportError:
        return None


def _dt_to_str(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt_from_str(s: Any) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def _bundle_to_jsonable(bundle: DetectionBundle) -> dict:
    """``Cache.put()`` serialises with ``json.dumps(..., default=str)`` -- a raw dataclass would
    degrade to its ``str()`` in there. Hence the manual decomposition into JSON-friendly basic
    types (and the reassembly in ``_bundle_from_jsonable``)."""
    det = bundle.det
    hw = det.history_window
    tz = det.timezone
    return {
        "det": {
            "profile_name": det.profile_name,
            "classification": det.classification.value,
            "server_version": det.server_version,
            "database_name": det.database_name,
            "connected_as": det.connected_as,
            "session_is_read_only": det.session_is_read_only,
            "installation_id": det.installation_id,
            "engine_schema_version": det.engine_schema_version,
            "schema_log": [[v, _dt_to_str(ts)] for v, ts in det.schema_log],
            "flyway_migrations": [[v, d, _dt_to_str(ts)] for v, d, ts in det.flyway_migrations],
            "history_level": det.history_level.value if det.history_level else None,
            "history_level_raw": det.history_level_raw,
            "history_window": None if hw is None else {
                "first_start": _dt_to_str(hw.first_start),
                "last_start": _dt_to_str(hw.last_start),
                "last_end": _dt_to_str(hw.last_end),
                "running_instances": hw.running_instances,
                "removal_time_min": _dt_to_str(hw.removal_time_min),
                "removal_time_max": _dt_to_str(hw.removal_time_max),
                "rows_past_removal_time": hw.rows_past_removal_time,
                "instances_without_removal_time": hw.instances_without_removal_time,
            },
            "tables": [dataclasses.asdict(t) for t in det.tables],
            "features": [
                {**dataclasses.asdict(f), "feature": f.feature.value} for f in det.features
            ],
            "deviations": [dataclasses.asdict(d) for d in det.deviations],
            "tenant_ids": list(det.tenant_ids),
            "timezone": None if tz is None else {
                "db_now": _dt_to_str(tz.db_now),
                "db_timezone_setting": tz.db_timezone_setting,
                "latest_history_timestamp": _dt_to_str(tz.latest_history_timestamp),
                "lag_to_db_now_seconds": tz.lag_to_db_now_seconds,
                "start_hour_histogram": {str(k): v for k, v in tz.start_hour_histogram.items()},
                "configured_source_timezone": tz.configured_source_timezone,
                "configured_display_timezone": tz.configured_display_timezone,
            },
            "detected_at": _dt_to_str(det.detected_at),
            "duration_ms": det.duration_ms,
        },
        "proof": dataclasses.asdict(bundle.proof),
        "computed_at": _dt_to_str(bundle.computed_at),
    }


def _bundle_from_jsonable(d: dict) -> DetectionBundle:
    det_d = d["det"]
    h = det_d.get("history_window")
    hw = None if not h else HistoryWindow(
        first_start=_dt_from_str(h["first_start"]),
        last_start=_dt_from_str(h["last_start"]),
        last_end=_dt_from_str(h["last_end"]),
        running_instances=h["running_instances"],
        removal_time_min=_dt_from_str(h["removal_time_min"]),
        removal_time_max=_dt_from_str(h["removal_time_max"]),
        rows_past_removal_time=h["rows_past_removal_time"],
        instances_without_removal_time=h["instances_without_removal_time"],
    )
    t = det_d.get("timezone")
    tz = None if not t else TimezoneEvidence(
        db_now=_dt_from_str(t["db_now"]),
        db_timezone_setting=t["db_timezone_setting"],
        latest_history_timestamp=_dt_from_str(t["latest_history_timestamp"]),
        lag_to_db_now_seconds=t["lag_to_db_now_seconds"],
        start_hour_histogram={int(k): v for k, v in (t.get("start_hour_histogram") or {}).items()},
        configured_source_timezone=t["configured_source_timezone"],
        configured_display_timezone=t["configured_display_timezone"],
    )
    det = DetectionResult(
        profile_name=det_d["profile_name"],
        classification=Classification(det_d["classification"]),
        server_version=det_d["server_version"],
        database_name=det_d["database_name"],
        connected_as=det_d["connected_as"],
        session_is_read_only=det_d["session_is_read_only"],
        installation_id=det_d.get("installation_id"),
        engine_schema_version=det_d.get("engine_schema_version"),
        schema_log=[(v, _dt_from_str(ts)) for v, ts in det_d.get("schema_log") or []],
        flyway_migrations=[(v, dsc, _dt_from_str(ts)) for v, dsc, ts in det_d.get("flyway_migrations") or []],
        history_level=HistoryLevel(det_d["history_level"]) if det_d.get("history_level") is not None else None,
        history_level_raw=det_d.get("history_level_raw"),
        history_window=hw,
        tables=[TableInfo(**t) for t in det_d.get("tables") or []],
        features=[
            FeatureStatus(**{**f, "feature": Feature(f["feature"])})
            for f in det_d.get("features") or []
        ],
        deviations=[SchemaDeviation(**dv) for dv in det_d.get("deviations") or []],
        tenant_ids=det_d.get("tenant_ids") or [],
        timezone=tz,
        detected_at=_dt_from_str(det_d.get("detected_at")),
        duration_ms=det_d.get("duration_ms"),
    )
    proof = ProofInfo(**d["proof"])
    computed_at = _dt_from_str(d.get("computed_at")) or deps.now_utc()
    return DetectionBundle(det=det, proof=proof, computed_at=computed_at)


def _cache_read(profile_name: str) -> DetectionBundle | None:
    handle = _cache_handles.get(profile_name)
    if handle is None:
        return None
    try:
        entry = handle.get(_CACHE_KEY)
    except Exception as exc:  # noqa: BLE001 -- foreign module, and purely an optimisation
        log.info("cache read for '%s' not possible: %s", profile_name, exc)
        return None
    if entry is None:
        return None
    payload, _created_at = entry
    try:
        return _bundle_from_jsonable(payload)
    except Exception as exc:  # noqa: BLE001 -- corrupt or incompatible cache entry
        log.info("cache entry for '%s' not readable: %s", profile_name, exc)
        return None


def _cache_write(profile_name: str, det: DetectionResult, bundle: DetectionBundle) -> None:
    Cache = _cache_class()
    if Cache is None:
        return
    try:
        handle = Cache.for_detection(profile_name, det)
        handle.put(_CACHE_KEY, _bundle_to_jsonable(bundle))
        _cache_handles[profile_name] = handle
    except Exception as exc:  # noqa: BLE001 -- as above
        log.info("cache write for '%s' not possible: %s", profile_name, exc)


def _get_detection(profile: Profile, *, force: bool) -> tuple[DetectionBundle | None, str | None, bool]:
    """Return ``(bundle, error_message, came_from_cache)``. Exactly one of the first two is not
    None."""
    if not force:
        cached = _cache_read(profile.name)
        if cached is not None:
            return cached, None, True

    try:
        from ..db import detect as detect_mod  # type: ignore[import-not-found]
    except ImportError as exc:
        return None, (
            "The detection module (cib7explorer.db.detect) is not available "
            f"({exc}). Once it can be imported, the detection appears here automatically."
        ), False

    try:
        with deps.open_database(profile) as db:
            det = detect_mod.detect(db, profile)
            raw_proof = db.read_only_proof
            proof = ProofInfo(
                ok=raw_proof.ok,
                privileges_clean=raw_proof.privileges_clean,
                summary=raw_proof.summary,
                probed_table=raw_proof.probed_table,
            )
    except connection.NotReadOnly as exc:
        return None, str(exc), False
    except connection.QueryTimeout as exc:
        return None, str(exc), False
    except connection.DatabaseError as exc:
        return None, str(exc), False
    except (AttributeError, TypeError) as exc:
        # detect_mod exists but does not have the expected shape or signature.
        return None, f"The detection module is incomplete or incompatible: {exc}", False

    bundle = DetectionBundle(det=det, proof=proof, computed_at=deps.now_utc())
    _cache_write(profile.name, det, bundle)
    return bundle, None, False


# --- Restore -------------------------------------------------------------------------------

def _restore_module() -> Any:
    try:
        from ..restore import docker_restore as rst  # type: ignore[import-not-found]
        return rst
    except ImportError:
        return None


def _clean_message(exc: BaseException) -> str:
    """Never a traceback in a response -- only the already redacted message."""
    return config.redact(str(exc) or exc.__class__.__name__)


def _restore_current_status(profile: Profile) -> tuple[Any | None, str | None]:
    """Read the restore state for display -- including the fragment's two-second self-poll.

    ``docker_restore.status()`` reconciles the state with reality (is the container running, is
    the database reachable, does the fingerprint match), and to do that it re-hashes the dump
    file on every call. For a multi-gigabyte dump that is unsuitable for a two-second polling
    loop. While a restore is actively being followed (phase != ABSENT) the background run writes
    its progress into the state file anyway, so the cheap ``read_state()`` is enough. Only when
    *nothing* is known (ABSENT) is the more expensive reconciliation worth doing once, to detect
    a matching container that already exists.
    """
    rst = _restore_module()
    if rst is None:
        return None, "The restore module (cib7explorer.restore.docker_restore) is not available."
    try:
        state = rst.read_state(profile)
        if state.phase is RestorePhase.ABSENT:
            state = rst.status(profile)
        return state, None
    except Exception as exc:  # noqa: BLE001 -- foreign module, e.g. rst.RestoreError
        return None, _clean_message(exc)


def _render_restore_fragment(request: Request, profile: Profile, state: Any | None, error: str | None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "fragments/restore_status.html",
        {"profile": profile, "state": state, "error": error},
    )


# --- The detection fragment ---------------------------------------------------------------

def _render_detection_fragment(
    request: Request, profile: Profile, bundle: DetectionBundle | None, error: str | None, from_cache: bool
) -> HTMLResponse:
    sorted_tables = []
    as_of = None
    if bundle is not None:
        sorted_tables = sorted(
            bundle.det.tables,
            key=lambda t: t.total_bytes if t.total_bytes is not None else -1,
            reverse=True,
        )
        # `detected_at` is the meaningful timestamp -- when the detection actually *ran*.
        # `computed_at` (wall clock of this process) is only the fallback, in case detect.py ever
        # leaves that field unset.
        as_of = bundle.det.detected_at or bundle.computed_at
    return templates.TemplateResponse(
        request,
        "fragments/detection.html",
        {
            "profile": profile,
            "bundle": bundle,
            "det": bundle.det if bundle else None,
            "proof": bundle.proof if bundle else None,
            "error": error,
            "from_cache": from_cache,
            "feature_labels": FEATURE_LABELS,
            "sorted_tables": sorted_tables,
            "as_of": as_of,
        },
    )


# --- Routen --------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    if deps.no_profile_configured():
        return templates.TemplateResponse(
            request,
            "index_missing.html",
            {"expected_path": str(config.profiles_path())},
        )
    profiles = deps.load_profiles()
    docker_ok, docker_msg = deps.docker_available()
    rows = [profiles[name] for name in sorted(profiles)]
    try:
        from ..notes import default_notes

        mark_count = default_notes().count()
    except Exception:  # noqa: BLE001
        mark_count = 0
    return templates.TemplateResponse(
        request,
        "index.html",
        {"rows": rows, "docker_ok": docker_ok, "docker_msg": docker_msg,
            "profiles_file": str(config.profiles_path()),
            "mark_count": mark_count,
},
    )


@app.get("/profile/{name}", response_class=HTMLResponse)
def profile_page(request: Request, name: str) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    prod_warning = None
    if profile.classification is Classification.PROD:
        prod_warning = (
            "this connection points at a database classified as PROD. Without an allowlist the "
            "value mode stays off."
        )
    return templates.TemplateResponse(
        request,
        "profile.html",
        {"profile": profile, "prod_warning": prod_warning},
    )


@app.get("/profile/{name}/detection", response_class=HTMLResponse)
def detection(request: Request, name: str) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    bundle, error, from_cache = _get_detection(profile, force=False)
    return _render_detection_fragment(request, profile, bundle, error, from_cache)


@app.post("/profile/{name}/detection/rebuild", response_class=HTMLResponse)
def detection_rebuild(request: Request, name: str) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    bundle, error, from_cache = _get_detection(profile, force=True)
    return _render_detection_fragment(request, profile, bundle, error, from_cache)


@app.post("/profile/{name}/restore", response_class=HTMLResponse)
def restore_start(request: Request, name: str, force: bool = False) -> HTMLResponse:
    # `force` is a query parameter rather than a form field: FastAPI form parsing needs
    # `python-multipart`, which has no other use here -- a query parameter ("?force=true") is
    # enough for this single switch and saves the dependency.
    profile = deps.get_profile_or_404(name)
    rst = _restore_module()
    if rst is None:
        return _render_restore_fragment(
            request, profile, None,
            "The restore module (cib7explorer.restore.docker_restore) is not available.",
        )
    try:
        state = rst.start_background(profile, force=force)
    except Exception as exc:  # noqa: BLE001 -- e.g. rst.RestoreError, shape not guaranteed
        return _render_restore_fragment(request, profile, None, _clean_message(exc))
    return _render_restore_fragment(request, profile, state, None)


@app.get("/profile/{name}/restore/status", response_class=HTMLResponse)
def restore_status(request: Request, name: str) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    if profile.kind is not ProfileKind.LOCAL_RESTORE:
        # Restore only applies to the dump path -- for direct/tunnel profiles there is nothing
        # to show, and that is not an error.
        return HTMLResponse("")
    state, error = _restore_current_status(profile)
    return _render_restore_fragment(request, profile, state, error)


@app.get("/health")
def health() -> JSONResponse:
    profiles = deps.load_profiles()
    docker_ok, docker_msg = deps.docker_available()
    try:
        version = importlib.metadata.version("cib7explorer")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return JSONResponse({
        "version": version,
        "docker_available": docker_ok,
        "docker_message": docker_msg,
        "profiles_file": str(config.profiles_path()),
        "profiles_file_present": not deps.profiles_file_missing(),
        "profiles": [
            {"name": p.name, "kind": p.kind.value, "classification": p.classification.value}
            for p in profiles.values()
        ],
    })
