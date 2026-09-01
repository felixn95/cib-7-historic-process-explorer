"""The process landscape in numbers.

Two properties shape this module:

**Every number is clickable.** From every bar, every edge, every row there is a way to the
business keys behind it, and from there into the case view. That is what ``/landscape/cases``
does: it recomputes the case level for exactly the cell that was asked about -- about a second --
instead of keeping huge mappings in the cache.

**Every number carries its denominator and its time window.** Both appear at the top of the page
and next to the tables, not in the small print.
"""

from __future__ import annotations

import dataclasses
import logging
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from .. import config
from ..config import Profile
from ..contracts import Classification, Landscape
from ..jobs import registry
from . import deps
from .views_definitions import fmt_duration_ms, fmt_fraction, fmt_pct
from .views_cases import case_url

log = logging.getLogger("cib7explorer.web.views_landscape")

router = APIRouter()
_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
for _n, _f in (("num", deps.fmt_int), ("bytes_size", deps.fmt_bytes), ("datetime", deps.fmt_dt),
               ("ago", deps.fmt_ago), ("pct", fmt_pct), ("fraction", fmt_fraction),
               ("duration", fmt_duration_ms)):
    templates.env.filters[_n] = _f
templates.env.globals["static_version"] = deps.static_version
templates.env.globals["base_path"] = deps.base_path
templates.env.globals["auth_enabled"] = deps.auth_enabled

_LANDSCAPE_KIND = "landscape"
_CACHE_KEY = "landscape"

#: How many definitions the call graph draws. Drawing every one of them would be unreadable; the
#: table underneath always shows all of them, and the number of omitted edges is stated.
GRAPH_NODES_DEFAULT = 30


def _base(profile: Profile) -> dict[str, Any]:
    prod = None
    if profile.classification is Classification.PROD:
        prod = "this connection points at a database classified as PROD."
    return {"prod_warning": prod, "case_url": case_url, "gap_label": gap_label}


# --- Cache ------------------------------------------------------------------------------

def _cache_for(profile: Profile):
    from ..cache import Cache
    from ..db import connection, detect

    with deps.open_database(profile) as db:
        det = detect.detect(db, profile)
    return Cache.for_detection(profile.name, det), det


def _read(profile: Profile) -> tuple[Landscape | None, datetime | None, str | None]:
    try:
        cache, _ = _cache_for(profile)
        hit = cache.get(_CACHE_KEY)
    except Exception as exc:  # noqa: BLE001
        return None, None, config.redact(str(exc))
    if not hit:
        return None, None, None
    payload, created = hit
    try:
        return _from_dict(payload), created, None
    except Exception as exc:  # noqa: BLE001
        return None, created, f"Precomputation not readable ({exc.__class__.__name__}) — rebuild it."


def _from_dict(d: dict[str, Any]) -> Landscape:
    """The cache holds JSON, not objects -- this turns it back into the data types."""
    from ..contracts import (ActorStats, CallEdge, CoOccurrence, DisruptionStats, Distribution,
                             LandscapeMeta, MonthCount, SequencePattern, Transition)

    def dt(v):
        return datetime.fromisoformat(v) if isinstance(v, str) else v

    def dist(v):
        return Distribution(**v) if v else Distribution()

    meta = dict(d.get("meta") or {})
    meta["built_at"] = dt(meta.get("built_at"))
    meta["window_start"] = dt(meta.get("window_start"))
    meta["window_end"] = dt(meta.get("window_end"))
    meta["notes"] = tuple(meta.get("notes") or ())
    return Landscape(
        monthly=tuple(MonthCount(m["def_key"], dt(m["month"]), m["instances"], m["versions"])
                      for m in d.get("monthly") or ()),
        call_edges=tuple(CallEdge(**e) for e in d.get("call_edges") or ()),
        depth_distribution=tuple((int(a), int(b)) for a, b in d.get("depth_distribution") or ()),
        only_root=tuple(d.get("only_root") or ()),
        only_child=tuple(d.get("only_child") or ()),
        both_roles=tuple(d.get("both_roles") or ()),
        co_occurrence=tuple(CoOccurrence(**c) for c in d.get("co_occurrence") or ()),
        transitions=tuple(Transition(**t) for t in d.get("transitions") or ()),
        entry_defs=tuple((a, int(b)) for a, b in d.get("entry_defs") or ()),
        exit_defs=tuple((a, int(b)) for a, b in d.get("exit_defs") or ()),
        sequences=tuple(SequencePattern(sequence=tuple(s["sequence"]), count=s["count"],
                                       example_keys=tuple(s.get("example_keys") or ()))
                        for s in d.get("sequences") or ()),
        sequences_distinct=int(d.get("sequences_distinct") or 0),
        sequences_unique_once=int(d.get("sequences_unique_once") or 0),
        instances_per_key=dist(d.get("instances_per_key")),
        definitions_per_key=dist(d.get("definitions_per_key")),
        span_per_key_ms=dist(d.get("span_per_key_ms")),
        gaps_ms=dist(d.get("gaps_ms")),
        gap_counts=tuple((a, int(b)) for a, b in d.get("gap_counts") or ()),
        overlap_pairs=int(d.get("overlap_pairs") or 0),
        gap_pairs=int(d.get("gap_pairs") or 0),
        actors=ActorStats(**{**(d.get("actors") or {}),
                             "per_assignee": tuple(tuple(x) for x in
                                                   (d.get("actors") or {}).get("per_assignee") or ())}),
        disruptions=DisruptionStats(
            per_definition=tuple(tuple(x) for x in
                                 (d.get("disruptions") or {}).get("per_definition") or ()),
            historic_incidents_available=bool((d.get("disruptions") or {}).get("historic_incidents_available")),
            operation_log_available=bool((d.get("disruptions") or {}).get("operation_log_available"))),
        validation_only_instances=d.get("validation_only_instances"),
        meta=LandscapeMeta(**meta),
    )


def _build_job(profile: Profile):
    def run(progress) -> str:
        from ..db import connection, detect, landscape

        with deps.open_database(profile) as db:
            det = detect.detect(db, profile)
            progress.step("detection read")
            land = landscape.build_landscape(db, profile, detection=det, progress=progress)
        cache, _ = _cache_for(profile)
        cache.put(_CACHE_KEY, dataclasses.asdict(land),
                  source_note="process landscape")
        def number(n: int) -> str:
            return f"{n:,}"

        return (f"{land.meta.definitions_total} definitions, {number(land.meta.keys_total)} "
                f"cases, {number(land.sequences_distinct)} distinct sequences")

    return run


# --- pages ----------------------------------------------------------------------------------

@router.get("/profile/{name}/landscape", response_class=HTMLResponse)
def landscape_page(request: Request, name: str, graph: int = GRAPH_NODES_DEFAULT,
                     min_transition: int = 1, sequences: int = 40) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    land, created, error = _read(profile)
    job = registry.running(_LANDSCAPE_KIND, profile.name)
    ctx: dict[str, Any] = {
        "profile": profile, "request": request, "land": land, "built_at": created,
        "error": error, "job": job, "graph_nodes": max(5, min(graph, 80)),
        "min_transition": max(1, min_transition), "sequences": max(5, min(sequences, 400)),
        "graph": None, **_base(profile),
    }
    if land:
        ctx["graph"] = _build_graph(land, ctx["graph_nodes"])
    return templates.TemplateResponse(request, "landscape.html", ctx)


@router.post("/profile/{name}/landscape/rebuild", response_class=HTMLResponse)
def landscape_rebuild(request: Request, name: str) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    job = registry.start(_LANDSCAPE_KIND, profile.name, _build_job(profile), steps_total=13)
    return templates.TemplateResponse(request, "fragments/landscape_status.html",
                                      {"request": request, "profile": profile, "job": job})


@router.get("/profile/{name}/landscape/status", response_class=HTMLResponse)
def landscape_status(request: Request, name: str) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    job = registry.latest(_LANDSCAPE_KIND, profile.name)
    return templates.TemplateResponse(request, "fragments/landscape_status.html",
                                      {"request": request, "profile": profile, "job": job})


# --- drill-down: from any number to the cases behind it -------------------------------------

@router.get("/profile/{name}/landscape/cases", response_class=HTMLResponse)
def cases(request: Request, name: str, kind: str, a: str = "", b: str = "",
           month: str = "", sequence: str = "", limit: int = 300) -> HTMLResponse:
    """The business keys behind a number.

    Recomputed on every click (about a second) rather than stored: the complete cell-to-keys
    mapping would be several times the size of the precomputation, and the result would still be
    only as fresh as the cache.
    """
    profile = deps.get_profile_or_404(name)
    ctx: dict[str, Any] = {"profile": profile, "request": request, "kind": kind, "a": a, "b": b,
                           "month": month, "sequence": sequence, "keys": [], "title": "",
                           "error": None, "total": 0, "limit": limit, **_base(profile)}
    try:
        from ..db import landscape as mod

        with deps.open_database(profile) as db:
            keys, title, total = _resolve_cases(db, mod, kind, a, b, month, sequence, limit)
        ctx.update({"keys": keys, "title": title, "total": total})
    except Exception as exc:  # noqa: BLE001
        ctx["error"] = config.redact(str(exc))
    return templates.TemplateResponse(request, "fragments/landscape_cases.html", ctx)


def _resolve_cases(db, mod, kind: str, a: str, b: str, month: str, sequence: str, limit: int
                   ) -> tuple[list[tuple[str, int, datetime | None]], str, int]:
    """Recompute the case level for exactly the cell that was asked about."""
    roots = db.fetch(mod._SQL_ROOTS, limit=mod.MAX_ROOT_ROWS, timeout_ms=mod.BUILD_TIMEOUT_MS,
                     name="cases_roots")
    per_key: dict[str, list[tuple[str, datetime | None, datetime | None]]] = defaultdict(list)
    for bk, def_key, start, end, _state in roots.rows:
        per_key[bk].append((def_key or "(no key)", start, end))

    hits: list[str] = []
    title = ""
    if kind == "transition":
        title = f"Cases with the transition {a} → {b}"
        for bk, rows in per_key.items():
            sequence_defs = [r[0] for r in rows]
            if any(x == a and y == b for x, y in zip(sequence_defs, sequence_defs[1:])):
                hits.append(bk)
    elif kind == "sequence":
        wanted = tuple(x for x in sequence.split(">") if x)
        title = "Cases with the sequence " + " → ".join(wanted)
        for bk, rows in per_key.items():
            if tuple(r[0] for r in rows) == wanted:
                hits.append(bk)
    elif kind == "cooccurrence":
        title = f"Cases in which {a} and {b} both occur"
        res = db.fetch(mod._SQL_KEY_INSTANCES, limit=mod.MAX_INSTANCE_ROWS,
                       timeout_ms=mod.BUILD_TIMEOUT_MS, name="cases_key_instances")
        defs_per_key: dict[str, set[str]] = defaultdict(set)
        for bk, def_key, _s, _e in res.rows:
            defs_per_key[bk].add(def_key or "(no key)")
        hits = [bk for bk, defs in defs_per_key.items() if a in defs and b in defs]
    elif kind == "definition":
        title = f"Cases in which {a} occurs"
        res = db.fetch(mod._SQL_KEY_INSTANCES, limit=mod.MAX_INSTANCE_ROWS,
                       timeout_ms=mod.BUILD_TIMEOUT_MS, name="cases_key_instances")
        found: set[str] = set()
        for bk, def_key, _s, _e in res.rows:
            if (def_key or "(no key)") == a:
                found.add(bk)
        hits = sorted(found)
    elif kind == "month":
        title = f"Cases with {a} in {month}"
        for bk, rows in per_key.items():
            if any(r[0] == a and r[1] and r[1].strftime("%Y-%m") == month for r in rows):
                hits.append(bk)
    elif kind == "gap":
        from_time, to_time, label = _gap_range(a)
        title = f"Cases with a gap {label}"
        for bk, rows in per_key.items():
            hwm = None
            for (_da, _sa, ea), (_db, sb, _eb) in zip(rows, rows[1:]):
                hwm = ea if hwm is None or (ea and ea > hwm) else hwm
                if hwm and sb:
                    delta = (sb - hwm).total_seconds()
                    if from_time < delta <= to_time:
                        hits.append(bk)
                        break
    elif kind == "entry" or kind == "exit":
        title = ("Cases that " + ("begin" if kind == "entry" else "end") + " with " + a)
        for bk, rows in per_key.items():
            defs = [r[0] for r in rows]
            if defs and ((kind == "entry" and defs[0] == a) or (kind == "exit" and defs[-1] == a)):
                hits.append(bk)
    else:
        raise ValueError(f"Unknown drill-down: {kind!r}")

    total = len(hits)
    sizes = {bk: len(per_key.get(bk, ())) for bk in hits}
    hits.sort(key=lambda bk: -sizes.get(bk, 0))
    keys = [(bk, sizes.get(bk, 0),
             min((r[1] for r in per_key.get(bk, ()) if r[1]), default=None))
            for bk in hits[:limit]]
    return keys, title, total


def _gap_range(key: str) -> tuple[float, float, str]:
    from ..db.landscape import GAP_BUCKETS

    lower = 0.0
    for bucket_key, label, upper in GAP_BUCKETS:
        if bucket_key == key:
            return lower, upper, label
        lower = upper
    return 0.0, float("inf"), key


def gap_label(key: str) -> str:
    from ..db.landscape import GAP_BUCKETS

    return next((label for bucket_key, label, _ in GAP_BUCKETS if bucket_key == key), key)


# --- The call graph as SVG -----------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class GraphNode:
    key: str
    x: float
    y: float
    calls: int
    label_anchor: str


@dataclasses.dataclass(frozen=True)
class GraphEdge:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    calls: int
    parent: str
    child: str


@dataclasses.dataclass(frozen=True)
class Graph:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    shown_edges: int
    total_edges: int
    shown_calls: int
    total_calls: int


def _build_graph(land: Landscape, node_limit: int) -> Graph:
    """A circular layout. Deliberately not force-directed: that would need JavaScript, and the
    arrangement would suggest a proximity that is not in the data. The circle orders by call
    volume only -- the information sits in the edges, not in the positions."""
    import math

    volume: Counter[str] = Counter()
    for e in land.call_edges:
        volume[e.parent_def] += e.calls
        volume[e.child_def] += e.calls
    selected = [k for k, _ in volume.most_common(node_limit)]
    index = {k: i for i, k in enumerate(selected)}
    n = max(1, len(selected))
    r = 240.0
    positions: dict[str, tuple[float, float]] = {}
    nodes: list[GraphNode] = []
    for i, key in enumerate(selected):
        winkel = 2 * math.pi * i / n - math.pi / 2
        x, y = 300 + r * math.cos(winkel), 300 + r * math.sin(winkel)
        positions[key] = (x, y)
        nodes.append(GraphNode(key=key, x=x, y=y, calls=volume[key],
                               label_anchor="start" if math.cos(winkel) >= 0 else "end"))

    visible = [e for e in land.call_edges
                if e.parent_def in index and e.child_def in index]
    max_calls = max((e.calls for e in visible), default=1)
    edges = []
    for e in visible:
        x1, y1 = positions[e.parent_def]
        x2, y2 = positions[e.child_def]
        edges.append(GraphEdge(x1=x1, y1=y1, x2=x2, y2=y2,
                               width=round(0.4 + 4.6 * (e.calls / max_calls) ** 0.5, 2),
                               calls=e.calls, parent=e.parent_def, child=e.child_def))
    return Graph(nodes=tuple(nodes), edges=tuple(edges), shown_edges=len(edges),
                 total_edges=len(land.call_edges),
                 shown_calls=sum(e.calls for e in visible),
                 total_calls=sum(e.calls for e in land.call_edges))
