"""Process definitions and their variable catalogue.

These pages read two precomputed analyses from the local cache (``cib7explorer.cache``, keys
"definitions" and "variable_catalog") -- never live from the large history tables. When the cache
is empty, the page shows an invitation with a button that starts the build as a background job
(``cib7explorer.jobs.registry``), and the interface stays usable while it runs.

The query logic itself lives in ``cib7explorer.db.definitions`` and
``cib7explorer.db.varcatalog``. Both are imported lazily -- inside the background job, never at
module import -- so that this interface starts and its read routes stay usable even if one of
those modules fails to load. A pure read of an existing cache entry does not need them at all.

Cache addressing (the same chicken-and-egg problem as in ``web/app.py``):
``Cache.for_detection(profile_name, det)`` needs a fresh ``DetectionResult`` to compute the
schema fingerprint and therefore the cache file name. This module remembers the resolved cache
handle per profile in process memory (``_cache_handles``): the first page after a server start
resolves it once with a fast schema query -- not with the expensive catalogue build -- and every
later page in the same process reads the already addressed SQLite file. After a restart it begins
fresh, consistent with the same decision in app.py.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone as _dt_timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from starlette.templating import Jinja2Templates

from .. import config
from ..config import Profile
from ..contracts import (
    CatalogMeta,
    CrossProcessVariable,
    DefinitionSummary,
    DefinitionVersionRow,
    DurationStats,
    EndActivity,
    FirstWriteActivity,
    HistoryLevel,
    SerializationForm,
    SizeStats,
    VariableCatalog,
    VariableCatalogEntry,
)
from ..jobs import Job, Progress, registry
from . import deps

log = logging.getLogger("cib7explorer.web.a")

router = APIRouter()

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
templates.env.filters["num"] = deps.fmt_int
templates.env.filters["bytes_size"] = deps.fmt_bytes
templates.env.filters["datetime"] = deps.fmt_dt
templates.env.filters["ago"] = deps.fmt_ago

_CATALOG_KIND = "catalog"
_KEY_DEFINITIONS = "definitions"
_KEY_CATALOG = "variable_catalog"
_KEY_META = "catalog_meta"

_VARS_PAGE_SIZE = 200

#: Number of progress steps in the catalogue build (definitions, the seven queries of the
#: variable catalogue, the cache write) -- so that the percentage does not jump straight to
#: 99 % and then sit there.
_CATALOG_STEPS = 19


# --- formatting filters specific to these pages ---------------------------------------------

def fmt_pct(x: float | None) -> str:
    if x is None:
        return "not recorded"
    return f"{x * 100:.1f} %"


def fmt_fraction(numerator: int | None, denominator: int | None) -> str:
    """A share always carries its denominator: "8,240 of 8,375 instances (98.4 %)".

    A bare percentage invites the reader to imagine a denominator, and the imagined one is
    usually wrong.
    """
    if numerator is None or denominator is None:
        return "not recorded"
    if denominator <= 0:
        return f"{deps.fmt_int(numerator)} of {deps.fmt_int(denominator)} (not determined)"
    pct = 100.0 * numerator / denominator
    return f"{deps.fmt_int(numerator)} of {deps.fmt_int(denominator)} ({pct:.1f} %)"


def fmt_duration_ms(ms: int | None) -> str:
    """A readable duration -- never an average, see the DurationStats docstring."""
    if ms is None:
        return "not recorded"
    if ms < 0:
        ms = 0
    seconds = ms / 1000.0
    if seconds < 1:
        return f"{ms} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    total_seconds = int(seconds)
    minutes, sec = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes} min {sec} s" if sec else f"{minutes} min"
    hours, minute = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minute} min" if minute else f"{hours} h"
    days, hour = divmod(hours, 24)
    return f"{days} d {hour} h" if hour else f"{days} d"


templates.env.filters["pct"] = fmt_pct
templates.env.filters["fraction"] = fmt_fraction
templates.env.filters["duration"] = fmt_duration_ms
templates.env.globals["static_version"] = deps.static_version
templates.env.globals["base_path"] = deps.base_path
templates.env.globals["auth_enabled"] = deps.auth_enabled


def _qs(request: Request, **overrides: Any) -> str:
    """Build a query-string URL that keeps the existing parameters and overrides only the ones
    passed in (or removes them when the value is None).

    Filter and sort state live in the URL rather than in session state, so any page can be
    bookmarked, shared and reloaded and still show the same thing.
    """
    params = dict(request.query_params)
    for k, v in overrides.items():
        if v is None:
            params.pop(k, None)
        else:
            params[k] = str(v)
    query = urlencode(params)
    return f"?{query}" if query else ""


def _sort_arrow(request: Request, column: str) -> str:
    current = request.query_params.get("sort")
    if current != column:
        return ""
    return " ↓" if request.query_params.get("dir", "desc") == "desc" else " ↑"


def _sort_href(request: Request, column: str, default_dir: str = "desc") -> str:
    current = request.query_params.get("sort")
    current_dir = request.query_params.get("dir", "desc")
    if current == column:
        next_dir = "asc" if current_dir == "desc" else "desc"
    else:
        next_dir = default_dir
    return _qs(request, sort=column, dir=next_dir, page=None)


templates.env.globals["qs"] = _qs
templates.env.globals["sort_href"] = _sort_href
templates.env.globals["sort_arrow"] = _sort_arrow


# --- Addressing the cache ----------------------------------------------------------------------

_cache_handles: dict[str, Any] = {}


def _cache_class() -> Any:
    try:
        from ..cache import Cache  # type: ignore[import-not-found]
        return Cache
    except ImportError:
        return None


def _get_cache(profile: Profile) -> tuple[Any | None, str | None]:
    """Return ``(cache handle, error message)``. See the module docstring on addressing."""
    Cache = _cache_class()
    if Cache is None:
        return None, "The cache module (cib7explorer.cache) is not available."

    handle = _cache_handles.get(profile.name)
    if handle is not None:
        return handle, None

    try:
        from ..db import detect as detect_mod  # type: ignore[import-not-found]
    except ImportError as exc:
        return None, (
            f"The detection module (cib7explorer.db.detect) is missing -- without a detection the "
            f"catalogue cache cannot be addressed ({exc})."
        )

    try:
        with deps.open_database(profile) as db:
            det = detect_mod.detect(db, profile)
    except Exception as exc:  # noqa: BLE001 -- every connection failure becomes one message
        return None, f"Connecting to the database failed: {config.redact(str(exc))}"

    try:
        handle = Cache.for_detection(profile.name, det)
    except Exception as exc:  # noqa: BLE001 -- third-party module
        return None, f"The cache could not be addressed: {exc}"

    _cache_handles[profile.name] = handle
    return handle, None


# --- serialising the contract objects to and from the cache ---------------------------------
#
# Cache.put() serialises with json.dumps(default=str); dataclasses.asdict() is enough on the write
# side (nested dataclasses and tuples already become dicts and lists there). On the read side,
# json.loads() returns only basic types -- timestamps as text, nested objects as dict and
# list -- so they are rebuilt by hand here (the same pattern as _bundle_from_jsonable in
# app.py).

def _dt_from_iso(s: Any) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def _end_activity_from_dict(d: dict) -> EndActivity:
    return EndActivity(**d)


def _duration_from_dict(d: dict | None) -> DurationStats:
    return DurationStats(**d) if d else DurationStats()


def _definition_from_dict(d: dict) -> DefinitionSummary:
    d = dict(d)
    d["first_start"] = _dt_from_iso(d.get("first_start"))
    d["last_start"] = _dt_from_iso(d.get("last_start"))
    d["last_end"] = _dt_from_iso(d.get("last_end"))
    d["duration"] = _duration_from_dict(d.get("duration"))
    d["end_activities"] = tuple(_end_activity_from_dict(e) for e in d.get("end_activities") or [])
    return DefinitionSummary(**d)


def _size_stats_from_dict(d: dict | None) -> SizeStats:
    return SizeStats(**d) if d else SizeStats()


def _serialization_from_dict(d: dict) -> SerializationForm:
    return SerializationForm(**d)


def _first_write_from_dict(d: dict) -> FirstWriteActivity:
    return FirstWriteActivity(**d)


def _variable_entry_from_dict(d: dict) -> VariableCatalogEntry:
    d = dict(d)
    d["types"] = tuple(d.get("types") or ())
    d["first_write_activities"] = tuple(
        _first_write_from_dict(x) for x in d.get("first_write_activities") or []
    )
    d["serialization"] = tuple(_serialization_from_dict(x) for x in d.get("serialization") or [])
    d["inline_size"] = _size_stats_from_dict(d.get("inline_size"))
    d["bytearray_size"] = _size_stats_from_dict(d.get("bytearray_size"))
    d["first_seen"] = _dt_from_iso(d.get("first_seen"))
    d["last_seen"] = _dt_from_iso(d.get("last_seen"))
    return VariableCatalogEntry(**d)


def _cross_from_dict(d: dict) -> CrossProcessVariable:
    d = dict(d)
    d["definitions"] = tuple(d.get("definitions") or ())
    d["types"] = tuple(d.get("types") or ())
    return CrossProcessVariable(**d)


def _catalog_from_dict(d: dict) -> VariableCatalog:
    return VariableCatalog(
        entries=tuple(_variable_entry_from_dict(e) for e in d.get("entries") or []),
        cross_process=tuple(_cross_from_dict(c) for c in d.get("cross_process") or []),
    )


def _meta_from_dict(d: dict) -> CatalogMeta:
    d = dict(d)
    d["built_at"] = _dt_from_iso(d.get("built_at"))
    d["notes"] = tuple(d.get("notes") or ())
    return CatalogMeta(**d)


@dataclass
class _CacheRead:
    """What a page needs from the cache for its header line ("Catalogue built ...")."""

    data: Any
    built_at: datetime | None
    age_seconds: float | None
    meta: CatalogMeta | None


def _read_cache_entry(
    cache: Any, key: str, from_dict: Callable[[Any], Any]
) -> _CacheRead | None:
    try:
        entry = cache.get(key)
    except Exception as exc:  # noqa: BLE001 -- third-party module
        log.info("cache read of '%s' failed: %s", key, exc)
        return None
    if entry is None:
        return None
    payload, created_at = entry
    try:
        data = from_dict(payload)
    except Exception as exc:  # noqa: BLE001 -- a broken or incompatible cache entry
        log.info("cache entry '%s' not readable: %s", key, exc)
        return None
    age = None
    try:
        delta = cache.age(key)
        age = delta.total_seconds() if delta is not None else None
    except Exception:  # noqa: BLE001
        pass
    meta = None
    try:
        meta_entry = cache.get(_KEY_META)
        if meta_entry is not None:
            meta = _meta_from_dict(meta_entry[0])
    except Exception:  # noqa: BLE001
        pass
    return _CacheRead(data=data, built_at=created_at, age_seconds=age, meta=meta)


def _read_definitions(cache: Any) -> _CacheRead | None:
    return _read_cache_entry(
        cache, _KEY_DEFINITIONS,
        lambda payload: [_definition_from_dict(d) for d in payload],
    )


def _read_catalog(cache: Any) -> _CacheRead | None:
    return _read_cache_entry(cache, _KEY_CATALOG, _catalog_from_dict)


# --- background job: (re)build the catalogue ------------------------------------------------

def _build_meta(
    profile: Profile, det: Any, defs: list[DefinitionSummary], catalog: VariableCatalog,
    duration_ms: int | None,
) -> CatalogMeta:
    notes: list[str] = []
    history_level_txt = None
    if det is not None and getattr(det, "history_level", None) is not None:
        history_level_txt = det.history_level.label
        if det.history_level is not HistoryLevel.FULL:
            notes.append(
                f"History level is {det.history_level.label} ({det.history_level.value}) -- "
                "act_hi_incident (historic incidents) and act_hi_detail (variable change "
                "history) are only written from FULL onwards, so they may be empty here -- "
                "which is not the same as 'zero incidents'."
            )
    notes.append(
        "This catalogue contains no variable values -- neither names, types nor sizes reveal "
        "any content."
    )
    installation_id = getattr(det, "installation_id", None) if det is not None else None
    return CatalogMeta(
        built_at=deps.now_utc(),
        duration_ms=duration_ms,
        profile_name=profile.name,
        installation_id=installation_id,
        history_level=history_level_txt,
        rows=len(catalog.entries),
        notes=tuple(notes),
    )


def _run_catalog_build(profile: Profile) -> Callable[[Progress], str]:
    def _run(progress: Progress) -> str:
        started = deps.now_utc()
        progress.step("connecting and determining the schema ...")
        try:
            from ..db import detect as detect_mod  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                f"The detection module (cib7explorer.db.detect) is missing ({exc})."
            ) from exc

        Cache = _cache_class()
        if Cache is None:
            raise RuntimeError("The cache module (cib7explorer.cache) is missing.")

        with deps.open_database(profile) as db:
            det = detect_mod.detect(db, profile)
            cache = Cache.for_detection(profile.name, det)
            _cache_handles[profile.name] = cache

            progress.step("loading process definitions ...")
            try:
                from ..db import definitions as defs_mod  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    f"The definitions module (cib7explorer.db.definitions) is missing ({exc})."
                ) from exc
            defs = defs_mod.fetch_definitions(db, profile, detection=det, progress=progress)

            progress.step("building the variable catalogue ...")
            try:
                from ..db import varcatalog as vc_mod  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    f"The variable catalogue module (cib7explorer.db.varcatalog) is missing ({exc})."
                ) from exc
            catalog = vc_mod.build_catalog(db, profile, detection=det, progress=progress)

        progress.step("writing to the cache ...")
        duration_ms = int((deps.now_utc() - started).total_seconds() * 1000)
        cache.put(_KEY_DEFINITIONS, [dataclasses.asdict(d) for d in defs],
                  source_note="process definitions")
        cache.put(_KEY_CATALOG, dataclasses.asdict(catalog),
                  source_note="variable catalogue")
        meta = _build_meta(profile, det, defs, catalog, duration_ms)
        cache.put(_KEY_META, dataclasses.asdict(meta), source_note="catalogue metadata")
        return f"{len(defs)} definitions, {len(catalog.entries)} variable rows"

    return _run


def _render_catalog_status_fragment(request: Request, profile: Profile, job: Job | None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "fragments/catalog_status.html",
        {"profile": profile, "job": job},
    )


@router.post("/profile/{name}/catalog/rebuild", response_class=HTMLResponse)
def catalog_rebuild(request: Request, name: str) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    job = registry.start(_CATALOG_KIND, profile.name, _run_catalog_build(profile), steps_total=_CATALOG_STEPS)
    return _render_catalog_status_fragment(request, profile, job)


@router.get("/profile/{name}/catalog/status", response_class=HTMLResponse)
def catalog_status(request: Request, name: str) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    job = registry.latest(_CATALOG_KIND, profile.name)
    return _render_catalog_status_fragment(request, profile, job)


def _page_context(profile: Profile, *, sort: str = "", dir: str = "") -> dict:
    """What every page here needs: sort state for the column links, plus the warning banner if
    the profile is classified as a production system."""
    from ..contracts import Classification

    prod_warning = None
    if profile.classification is Classification.PROD:
        prod_warning = (
            "this connection points at a database classified as PROD. These pages show no "
            "variable values in any case."
        )
    return {"prod_warning": prod_warning, "sort": sort, "dir": dir}


def _catalog_context(profile: Profile, read: _CacheRead | None, cache_error: str | None) -> dict:
    running = registry.running(_CATALOG_KIND, profile.name)
    age_ago = deps.fmt_ago(read.age_seconds) if read else None
    return {
        "cache_error": cache_error,
        "has_data": read is not None,
        "built_at": read.built_at if read else None,
        "age_ago": age_ago,
        "meta": read.meta if read else None,
        "job": running,
    }


# --- process definitions: the list ----------------------------------------------------------

_DEF_SORT_KEYS: dict[str, Callable[[DefinitionSummary], Any]] = {
    "instances": lambda d: d.instances,
    "key": lambda d: d.key.lower(),
    "first": lambda d: (d.first_start is None, d.first_start or datetime.min.replace(tzinfo=_dt_timezone.utc)),
    "last": lambda d: (d.last_start is None, d.last_start or datetime.min.replace(tzinfo=_dt_timezone.utc)),
    "duration": lambda d: (d.duration.p50 is None, d.duration.p50 or 0),
    "incidents": lambda d: d.open_incidents if d.open_incidents is not None else (
        d.historic_incidents if d.historic_incidents is not None else -1
    ),
    "terminated": lambda d: d.externally_terminated + d.internally_terminated,
    "validation": lambda d: (d.validation_flag_true or 0, d.validation_flag_share or 0),
}


def _filter_definitions(
    defs: Iterable[DefinitionSummary], *, q: str | None, role: str, ran: str,
) -> list[DefinitionSummary]:
    out = list(defs)
    if q:
        needle = q.strip().lower()
        out = [d for d in out if needle in d.key.lower() or (d.name and needle in d.name.lower())]
    if role == "root_only":
        out = [d for d in out if d.only_as_root]
    elif role == "child_only":
        out = [d for d in out if d.only_as_child]
    elif role == "both":
        out = [d for d in out if d.both_roles]
    if ran == "with_instances":
        out = [d for d in out if d.instances > 0]
    elif ran == "without_instances":
        out = [d for d in out if d.instances == 0]
    return out


def _sort_definitions(defs: list[DefinitionSummary], sort: str, direction: str) -> list[DefinitionSummary]:
    key_fn = _DEF_SORT_KEYS.get(sort, _DEF_SORT_KEYS["instances"])
    return sorted(defs, key=key_fn, reverse=(direction != "asc"))


@router.get("/profile/{name}/definitions", response_class=HTMLResponse)
def definitions(
    request: Request, name: str,
    sort: str = "instances", dir: str = "desc", q: str = "",
    role: str = "all", ran: str = "all",
) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    cache, cache_error = _get_cache(profile)
    read = _read_definitions(cache) if cache is not None else None

    rows: list[DefinitionSummary] = []
    if read is not None:
        rows = _filter_definitions(read.data, q=q, role=role, ran=ran)
        rows = _sort_definitions(rows, sort, dir)

    ctx = {
        "profile": profile, "request": request,
        "rows": rows, "total": len(rows),
        "total_unfiltered": len(read.data) if read else 0,
        "q": q, "role": role, "ran": ran,
        **_page_context(profile, sort=sort, dir=dir),
        **_catalog_context(profile, read, cache_error),
    }
    return templates.TemplateResponse(request, "definitions.html", ctx)


@router.get("/profile/{name}/definitions.csv")
def definitions_csv(
    request: Request, name: str,
    sort: str = "instances", dir: str = "desc", q: str = "",
    role: str = "all", ran: str = "all",
) -> Response:
    profile = deps.get_profile_or_404(name)
    cache, cache_error = _get_cache(profile)
    read = _read_definitions(cache) if cache is not None else None
    rows: list[DefinitionSummary] = []
    if read is not None:
        rows = _filter_definitions(read.data, q=q, role=role, ran=ran)
        rows = _sort_definitions(rows, sort, dir)

    fieldnames = [
        "key", "name", "deployed", "deployed_versions", "latest_deployed_version",
        "versions_used", "instances", "instances_as_root", "instances_as_child",
        "completed", "externally_terminated", "internally_terminated", "active",
        "first_start", "last_start", "last_end",
        "distinct_business_keys", "instances_without_business_key",
        "duration_p25_ms", "duration_p50_ms", "duration_p75_ms", "duration_p90_ms", "duration_max_ms",
        "open_incidents", "historic_incidents", "user_task_instances", "distinct_assignees",
    ]
    csv_rows = []
    for d in rows:
        csv_rows.append({
            "key": d.key, "name": d.name or "", "deployed": d.deployed,
            "deployed_versions": d.deployed_versions, "latest_deployed_version": d.latest_deployed_version,
            "versions_used": d.versions_used, "instances": d.instances,
            "instances_as_root": d.instances_as_root, "instances_as_child": d.instances_as_child,
            "completed": d.completed, "externally_terminated": d.externally_terminated,
            "internally_terminated": d.internally_terminated, "active": d.active,
            "first_start": d.first_start.isoformat() if d.first_start else "",
            "last_start": d.last_start.isoformat() if d.last_start else "",
            "last_end": d.last_end.isoformat() if d.last_end else "",
            "distinct_business_keys": d.distinct_business_keys,
            "instances_without_business_key": d.instances_without_business_key,
            "duration_p25_ms": d.duration.p25, "duration_p50_ms": d.duration.p50,
            "duration_p75_ms": d.duration.p75, "duration_p90_ms": d.duration.p90,
            "duration_max_ms": d.duration.maximum,
            "open_incidents": "" if d.open_incidents is None else d.open_incidents,
            "historic_incidents": "" if d.historic_incidents is None else d.historic_incidents,
            "user_task_instances": "" if d.user_task_instances is None else d.user_task_instances,
            "distinct_assignees": "" if d.distinct_assignees is None else d.distinct_assignees,
        })
    text = _write_csv(csv_rows, fieldnames)
    filename = f"definitions_{profile.name}_{datetime.now().strftime('%Y-%m-%d')}.csv"
    return Response(
        content=text, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _write_csv(rows: list[dict], fieldnames: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, delimiter=";")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return "﻿" + buf.getvalue()


# --- one process definition: detail ---------------------------------------------------------

@router.get("/profile/{name}/definitions/{key}", response_class=HTMLResponse)
def definition_detail(request: Request, name: str, key: str) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    cache, cache_error = _get_cache(profile)
    read = _read_definitions(cache) if cache is not None else None

    definition = None
    if read is not None:
        for d in read.data:
            if d.key == key:
                definition = d
                break
        if definition is None:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Definition '{key}' does not appear in the catalogue of profile "
                    f"'{profile.name}'. Either a typo, or the catalogue is stale -- "
                    "try rebuilding it."
                ),
            )

    catalog_read = _read_catalog(cache) if cache is not None else None
    variables: list[VariableCatalogEntry] = []
    if catalog_read is not None:
        variables = [e for e in catalog_read.data.entries if e.def_key == key]
        variables.sort(key=lambda e: (e.share_of_instances or 0, e.occurrences), reverse=True)

    versions: list[DefinitionVersionRow] = []
    versions_error: str | None = None
    if definition is not None:
        try:
            from ..db import definitions as defs_mod  # type: ignore[import-not-found]
        except ImportError as exc:
            versions_error = f"The definitions module (cib7explorer.db.definitions) is missing ({exc})."
        else:
            try:
                with deps.open_database(profile) as db:
                    versions = defs_mod.fetch_versions(db, key, limit=1000)
            except Exception as exc:  # noqa: BLE001 -- foreign module or connection failure
                versions_error = f"Version breakdown not available: {config.redact(str(exc))}"

    ctx = {
        "profile": profile, "request": request,
        "definition": definition, "key": key,
        **_page_context(profile),
        "variables": variables, "versions": versions, "versions_error": versions_error,
        **_catalog_context(profile, read, cache_error),
    }
    return templates.TemplateResponse(request, "definition_detail.html", ctx)


# --- The variable catalogue: the list ------------------------------------------------------------------

_VAR_SORT_KEYS: dict[str, Callable[[VariableCatalogEntry], Any]] = {
    "share": lambda e: e.share_of_instances or 0,
    "name": lambda e: e.name.lower(),
    "def_key": lambda e: e.def_key.lower(),
    "occurrences": lambda e: e.occurrences,
    "first_seen": lambda e: (e.first_seen is None, e.first_seen or datetime.min.replace(tzinfo=_dt_timezone.utc)),
    "last_seen": lambda e: (e.last_seen is None, e.last_seen or datetime.min.replace(tzinfo=_dt_timezone.utc)),
}


def _filter_variables(
    entries: Iterable[VariableCatalogEntry], *, q: str, def_key: str, type_change: str,
    min_share: float | None, scope: str,
) -> list[VariableCatalogEntry]:
    out = list(entries)
    if q:
        needle = q.strip().lower()
        out = [e for e in out if needle in e.name.lower()]
    if def_key:
        out = [e for e in out if e.def_key == def_key]
    if type_change == "1":
        out = [e for e in out if e.type_switch]
    elif type_change == "0":
        out = [e for e in out if not e.type_switch]
    if min_share is not None:
        out = [e for e in out if e.share_of_instances is not None and e.share_of_instances * 100 >= min_share]
    if scope == "instance":
        out = [e for e in out if e.scope_process_instance > 0]
    elif scope == "below_instance":
        out = [e for e in out if e.scope_below_process_instance > 0]
    elif scope == "task":
        out = [e for e in out if e.scope_task_local > 0]
    return out


def _sort_variables(entries: list[VariableCatalogEntry], sort: str, direction: str) -> list[VariableCatalogEntry]:
    key_fn = _VAR_SORT_KEYS.get(sort, _VAR_SORT_KEYS["share"])
    return sorted(entries, key=key_fn, reverse=(direction != "asc"))


def _paginate(items: list, page: int, page_size: int = _VARS_PAGE_SIZE) -> tuple[list, int, int, int]:
    total = len(items)
    total_pages = max(1, -(-total // page_size)) if page_size else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return items[start:start + page_size], page, total_pages, total


@router.get("/profile/{name}/variables", response_class=HTMLResponse)
def variables(
    request: Request, name: str,
    q: str = "", def_key: str = "", type_change: str = "", min_share: str = "",
    scope: str = "all", sort: str = "share", dir: str = "desc", page: int = 1,
) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    cache, cache_error = _get_cache(profile)
    read = _read_catalog(cache) if cache is not None else None

    min_share_val: float | None = None
    if min_share:
        try:
            min_share_val = float(min_share.replace(",", "."))
        except ValueError:
            min_share_val = None

    rows: list[VariableCatalogEntry] = []
    total_all = 0
    if read is not None:
        total_all = len(read.data.entries)
        rows = _filter_variables(
            read.data.entries, q=q, def_key=def_key, type_change=type_change,
            min_share=min_share_val, scope=scope,
        )
        rows = _sort_variables(rows, sort, dir)

    page_rows, page, total_pages, total_filtered = _paginate(rows, page)

    ctx = {
        "profile": profile, "request": request,
        "rows": page_rows, "total_filtered": total_filtered, "total_all": total_all,
        "page": page, "total_pages": total_pages, "page_size": _VARS_PAGE_SIZE,
        "q": q, "def_key": def_key, "type_change": type_change, "min_share": min_share,
        "scope": scope, "mode": "catalog",
        **_page_context(profile, sort=sort, dir=dir),
        "cross_rows": [],
        **_catalog_context(profile, read, cache_error),
    }
    return templates.TemplateResponse(request, "variables.html", ctx)


@router.get("/profile/{name}/variables/cross-process", response_class=HTMLResponse)
def variables_cross_process(request: Request, name: str, sort: str = "def_count", dir: str = "desc") -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    cache, cache_error = _get_cache(profile)
    read = _read_catalog(cache) if cache is not None else None

    cross_rows: list[CrossProcessVariable] = []
    if read is not None:
        cross_rows = list(read.data.cross_process)
        if sort == "occurrences":
            cross_rows.sort(key=lambda c: c.occurrences, reverse=(dir != "asc"))
        elif sort == "name":
            cross_rows.sort(key=lambda c: c.name.lower(), reverse=(dir != "asc"))
        else:
            cross_rows.sort(key=lambda c: (c.def_count, c.occurrences), reverse=(dir != "asc"))

    ctx = {
        "profile": profile, "request": request,
        "rows": [], "total_filtered": 0, "total_all": len(read.data.entries) if read else 0,
        "page": 1, "total_pages": 1, "page_size": _VARS_PAGE_SIZE,
        "q": "", "def_key": "", "type_change": "", "min_share": "", "scope": "all",
        "mode": "cross_process", "cross_rows": cross_rows,
        **_page_context(profile, sort=sort, dir=dir),
        **_catalog_context(profile, read, cache_error),
    }
    return templates.TemplateResponse(request, "variables.html", ctx)


@router.get("/profile/{name}/variables.csv")
def variables_csv(
    request: Request, name: str,
    q: str = "", def_key: str = "", type_change: str = "", min_share: str = "",
    scope: str = "all", sort: str = "share", dir: str = "desc",
) -> Response:
    profile = deps.get_profile_or_404(name)
    cache, cache_error = _get_cache(profile)
    read = _read_catalog(cache) if cache is not None else None

    if read is None:
        text = "﻿no catalogue in the cache -- build it first\r\n"
        filename = f"variables_{profile.name}_{datetime.now().strftime('%Y-%m-%d')}.csv"
        return Response(
            content=text, media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    min_share_val: float | None = None
    if min_share:
        try:
            min_share_val = float(min_share.replace(",", "."))
        except ValueError:
            min_share_val = None

    filtered = _filter_variables(
        read.data.entries, q=q, def_key=def_key, type_change=type_change,
        min_share=min_share_val, scope=scope,
    )
    filtered = _sort_variables(filtered, sort, dir)
    filtered_catalog = dataclasses.replace(read.data, entries=tuple(filtered))

    text: str | None = None
    try:
        from ..db import varcatalog as vc_mod  # type: ignore[import-not-found]
        text = vc_mod.to_csv(filtered_catalog, delimiter=";", bom=True)
    except (ImportError, AttributeError) as exc:
        log.info("varcatalog.to_csv unavailable (%s), falling back to the local CSV export.", exc)

    if text is None:
        text = _fallback_variables_csv(filtered)

    filename = f"variables_{profile.name}_{datetime.now().strftime('%Y-%m-%d')}.csv"
    return Response(
        content=text, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _fallback_variables_csv(entries: list[VariableCatalogEntry]) -> str:
    """Local CSV export in case ``varcatalog.to_csv`` is unavailable -- degrade rather than let
    the route crash (see the module docstring: foreign modules lazily and tolerantly)."""
    fieldnames = [
        "def_key", "name", "types", "type_switch", "occurrences", "instances_with",
        "def_instances", "null_typed", "instances_multi_scope",
        "scope_process_instance", "scope_below_process_instance", "scope_task_local",
        "first_seen", "last_seen", "version_min", "version_max", "in_latest_used_version",
    ]
    rows = []
    for e in entries:
        rows.append({
            "def_key": e.def_key, "name": e.name, "types": "|".join(e.types),
            "type_switch": e.type_switch, "occurrences": e.occurrences,
            "instances_with": e.instances_with, "def_instances": e.def_instances,
            "null_typed": e.null_typed, "instances_multi_scope": e.instances_multi_scope,
            "scope_process_instance": e.scope_process_instance,
            "scope_below_process_instance": e.scope_below_process_instance,
            "scope_task_local": e.scope_task_local,
            "first_seen": e.first_seen.isoformat() if e.first_seen else "",
            "last_seen": e.last_seen.isoformat() if e.last_seen else "",
            "version_min": e.version_min, "version_max": e.version_max,
            "in_latest_used_version": "" if e.in_latest_used_version is None else e.in_latest_used_version,
        })
    return _write_csv(rows, fieldnames)
