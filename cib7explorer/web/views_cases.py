"""Search, browse list and the timeline of a case.

The timeline is the actual view; the table is the second opinion. And the gaps are not the space
between things, they are the subject. The geometry is computed in Python rather than in the
template: percentage positions can be unit-tested, Jinja arithmetic cannot.

The unit of the timeline is the **call tree**, not the instance. A busy case easily holds twice as
many instances as trees, and a per-instance timeline stops being readable long before a per-tree
one does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from .. import config
from ..config import Profile
from ..contracts import Classification, GapKind, InstanceNode, TreeSummary, Case
from . import deps

log = logging.getLogger("cib7explorer.web.views_cases")

router = APIRouter()
_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

# The same filters as the definition pages use -- formatting should look identical everywhere.
from .views_definitions import fmt_duration_ms, fmt_fraction, fmt_pct  # noqa: E402

templates.env.filters["num"] = deps.fmt_int
templates.env.filters["bytes_size"] = deps.fmt_bytes
templates.env.filters["datetime"] = deps.fmt_dt
templates.env.filters["ago"] = deps.fmt_ago
templates.env.filters["pct"] = fmt_pct
templates.env.filters["fraction"] = fmt_fraction
templates.env.filters["duration"] = fmt_duration_ms
templates.env.globals["static_version"] = deps.static_version
templates.env.globals["base_path"] = deps.base_path
templates.env.globals["auth_enabled"] = deps.auth_enabled

#: The table shows no more instances than this at once. The total is stated next to it.
TABLE_PAGE = 300

#: Minimum bar width in percent, so that a sub-second instance does not become invisible.
MIN_BAR_PCT = 0.35


def from_display_tz(dt: datetime, profile: Profile) -> datetime:
    """Counterpart to ``deps.to_display_tz``: take a timestamp entered in the display zone and
    return the naive value as it is stored in the database.

    Necessary because the zoom fields accept what the page shows, while
    ``act_hi_procinst.start_time_`` is naive in the zone of the writing JVM. Without this
    conversion the window would be shifted by the zone offset -- two different zones on one page,
    which is exactly the confusion this tool is supposed to remove.
    """
    from zoneinfo import ZoneInfo

    aware = dt.replace(tzinfo=ZoneInfo(profile.display_timezone))
    return aware.astimezone(ZoneInfo(profile.source_timezone)).replace(tzinfo=None)


def _display(dt: datetime | None, profile: Profile, fmt: str) -> str:
    shown = deps.to_display_tz(dt, profile)
    return shown.strftime(fmt) if shown else ""


#: The format `<input type="datetime-local">` understands. Only this prefills the field -- with a
#: space instead of the T it stays empty, and the calendar opens on nothing.
FIELD_FORMAT = "%Y-%m-%dT%H:%M"


def _field_value(text: str, profile: Profile) -> str:
    """Bring an input -- with a space, or with seconds -- into the field format."""
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime((text or "").strip(), fmt).strftime(FIELD_FORMAT)
        except ValueError:
            continue
    return ""


def case_url(profile_name: str, key: str) -> str:
    """URL of a case.

    Business keys can contain a slash. Percent-encoding does not help, because the ASGI server
    decodes the path before routing and the path parameter would never match. So: a clean path URL
    for every ordinary key -- shareable, which matters -- and the query-parameter variant for the
    exceptions.
    """
    from urllib.parse import quote

    base = deps.with_base_path(f"/profile/{quote(profile_name)}/case")
    if "/" in key or "?" in key or "#" in key or "%" in key:
        return f"{base}?key={quote(key, safe='')}"
    return f"{base}/{quote(key, safe='')}"


def _page_context(profile: Profile) -> dict[str, Any]:
    prod_warning = None
    if profile.classification is Classification.PROD:
        prod_warning = ("this connection points at a database classified as PROD. The second "
                        "track (correlation over variable values) reads values and stays "
                        "hidden without an allowlist.")
    return {"prod_warning": prod_warning}


# --- geometry of the timeline ---------------------------------------------------------------

@dataclass(frozen=True)
class Bar:
    """One bar on the timeline, already expressed in percentages."""

    left_pct: float
    width_pct: float
    open_right: bool = False        # still running: the bar has no end
    open_left: bool = False         # begins before the window shown


@dataclass(frozen=True)
class TimelineRow:
    tree: TreeSummary
    bar: Bar
    instances: tuple[InstanceNode, ...]


@dataclass(frozen=True)
class GapBand:
    left_pct: float
    width_pct: float
    duration_ms: int | None
    kind: str
    start: datetime | None
    end: datetime | None
    after_def: str | None
    before_def: str | None


@dataclass(frozen=True)
class DensityBucket:
    left_pct: float
    width_pct: float
    height_pct: float
    trees: int
    start: datetime
    end: datetime

    #: Zoom parameters -- in the display zone, because the zoom fields expect display time too.
    from_time: str = ""
    to_time: str = ""


def _pct(value: datetime | None, start: datetime, span_s: float) -> float:
    if value is None or span_s <= 0:
        return 0.0
    return max(0.0, min(100.0, (value - start).total_seconds() / span_s * 100.0))


def build_bar(start: datetime | None, end: datetime | None,
              window_start: datetime, window_end: datetime, *, running: bool) -> Bar:
    span_s = (window_end - window_start).total_seconds()
    left = _pct(start, window_start, span_s)
    right = 100.0 if (running or end is None) else _pct(end, window_start, span_s)
    width = max(MIN_BAR_PCT, right - left)
    if left + width > 100.0:
        # Minimum width and edge clamping contradict each other at the right-hand edge. The bar
        # is shifted left rather than shrunk below visibility -- otherwise it either sticks out of
        # its lane or disappears entirely.
        left = max(0.0, 100.0 - width)
    return Bar(left_pct=round(left, 4), width_pct=round(width, 4),
               open_right=running or end is None,
               open_left=bool(start and start < window_start))


def build_timeline(v: Case, *, window: tuple[datetime, datetime] | None = None
                   ) -> tuple[list[TimelineRow], list[GapBand], tuple[datetime, datetime] | None]:
    """Compute lanes and gap bands as percentages of the window."""
    ws = (window[0] if window else v.window_start)
    we = (window[1] if window else v.window_end)
    if not ws or not we:
        return [], [], None
    if we <= ws:
        we = ws + timedelta(seconds=1)

    per_root: dict[str, list[InstanceNode]] = {}
    for node in v.instances:
        per_root.setdefault(node.root_id or node.proc_inst_id, []).append(node)

    rows: list[TimelineRow] = []
    for tree in v.trees:
        if tree.start_time and tree.start_time > we:
            continue
        if tree.end_time and tree.end_time < ws:
            continue
        members = sorted(per_root.get(tree.root_id, ()),
                         key=lambda n: (n.depth, n.start_time or datetime.max))
        rows.append(TimelineRow(
            tree=tree,
            bar=build_bar(tree.start_time, tree.end_time, ws, we, running=tree.running),
            instances=tuple(members),
        ))

    span_s = (we - ws).total_seconds()
    bands: list[GapBand] = []
    for gap in v.gaps:
        if gap.kind is not GapKind.BETWEEN or not gap.start or not gap.end:
            continue
        left = _pct(gap.start, ws, span_s)
        width = max(0.15, _pct(gap.end, ws, span_s) - left)
        bands.append(GapBand(
            left_pct=round(left, 4), width_pct=round(width, 4),
            duration_ms=gap.duration_ms, kind=gap.kind.value,
            start=gap.start, end=gap.end,
            after_def=gap.after_def, before_def=gap.before_def,
        ))
    return rows, bands, (ws, we)


@dataclass(frozen=True)
class Tick:
    left_pct: float
    label: str


def build_ticks(window_start: datetime, window_end: datetime, profile: Profile,
                *, count: int = 6) -> list[Tick]:
    """Axis labels. Without them a bar at 37 % says nothing.

    Most bars are hairlines: a median tree duration of well under a second inside a window
    spanning days is normal, so the information sits in the position, not in the width.
    """
    span_s = (window_end - window_start).total_seconds()
    if span_s <= 0:
        return []
    if span_s < 2 * 3600:
        fmt = "%H:%M"
    elif span_s < 3 * 86400:
        fmt = "%d.%m. %H:%M"
    elif span_s < 400 * 86400:
        fmt = "%d.%m."
    else:
        fmt = "%m/%Y"
    out: list[Tick] = []
    for i in range(count + 1):
        moment = window_start + timedelta(seconds=span_s * i / count)
        out.append(Tick(left_pct=round(100.0 * i / count, 4),
                        label=_display(moment, profile, fmt)))
    return out


def build_density(v: Case, profile: Profile, *, buckets: int = 80) -> list[DensityBucket]:
    """Density band across the lifetime of the key -- only useful above many root instances.

    For a typical case of a handful of instances it would be decoration; for a case with tens of
    thousands it is the only way to see any structure at all.
    """
    if not v.window_start or not v.window_end or len(v.trees) <= 1:
        return []
    span_s = (v.window_end - v.window_start).total_seconds() or 1.0
    counts = [0] * buckets
    for tree in v.trees:
        if not tree.start_time:
            continue
        idx = int((tree.start_time - v.window_start).total_seconds() / span_s * buckets)
        counts[min(max(idx, 0), buckets - 1)] += 1
    peak = max(counts) or 1
    width = 100.0 / buckets
    out: list[DensityBucket] = []
    for i, n in enumerate(counts):
        if not n:
            continue
        b_start = v.window_start + timedelta(seconds=span_s * i / buckets)
        b_end = v.window_start + timedelta(seconds=span_s * (i + 1) / buckets)
        out.append(DensityBucket(
            left_pct=round(i * width, 4), width_pct=round(width, 4),
            height_pct=round(100.0 * n / peak, 2), trees=n, start=b_start, end=b_end,
            from_time=_display(b_start, profile, FIELD_FORMAT),
            to_time=_display(b_end, profile, FIELD_FORMAT)))
    return out


# --- routes ---------------------------------------------------------------------------------

def _load_module():
    from ..db import case as mod
    return mod


@router.get("/profile/{name}/case", response_class=HTMLResponse)
def case_search(request: Request, name: str, q: str = "", match_mode: str = "contains",
                  browse: str = "instances", key: str = "", from_time: str = "", to_time: str = "",
                  table: int = 0, page: int = 1) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    if key:
        # Fallback route for keys that do not fit into a path segment (see case_url).
        return case_detail(request, name, key, from_time=from_time, to_time=to_time, table=table, page=page)
    ctx: dict[str, Any] = {
        "profile": profile, "request": request, "q": q, "match_mode": match_mode,
        "browse": browse, "hits": [], "rows_browse": [], "error": None,
        "orders": {}, "case_url": case_url, **_page_context(profile),
    }
    try:
        mod = _load_module()
    except ImportError as exc:
        ctx["error"] = f"The case module is not available: {exc}"
        return templates.TemplateResponse(request, "case_search.html", ctx)

    ctx["orders"] = mod.BROWSE_ORDERS
    try:
        with deps.open_database(profile) as db:
            if q.strip():
                ctx["hits"] = mod.search_keys(db, q, mode=match_mode, limit=200)
            ctx["rows_browse"] = mod.browse_keys(db, order=browse, limit=40)
    except Exception as exc:  # noqa: BLE001
        ctx["error"] = config.redact(str(exc))
    return templates.TemplateResponse(request, "case_search.html", ctx)


@router.get("/profile/{name}/case/{key}/correlation", response_class=HTMLResponse)
def correlation(request: Request, name: str, key: str) -> HTMLResponse:
    """The second track, loaded via HTMX: it reads variable values and takes noticeable time, so
    it is not part of the first render."""
    profile = deps.get_profile_or_404(name)
    ctx: dict[str, Any] = {"profile": profile, "request": request, "key": key,
                           "correlations": [], "error": None, "locked": None,
                           "case_url": case_url}
    if not profile.values_mode_effective:
        ctx["locked"] = (profile.values_mode_locked_reason
                         or "Value mode is off — this track reads variable values.")
        return templates.TemplateResponse(request, "fragments/case_correlation.html", ctx)
    try:
        mod = _load_module()
        with deps.open_database(profile) as db:
            v = mod.load_case(db, profile, key)
            ctx["correlations"] = mod.correlate(db, profile, v)
    except Exception as exc:  # noqa: BLE001
        ctx["error"] = config.redact(str(exc))
    return templates.TemplateResponse(request, "fragments/case_correlation.html", ctx)


@router.get("/profile/{name}/case/{key}", response_class=HTMLResponse)
def case_detail(request: Request, name: str, key: str, from_time: str = "", to_time: str = "",
                   table: int = 0, page: int = 1) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    ctx: dict[str, Any] = {
        "profile": profile, "request": request, "key": key, "case": None,
        "rows": [], "gap_bands": [], "density": [], "window": None, "error": None,
        "table": bool(table), "page": max(1, page), "table_page": TABLE_PAGE,
        "from_time": from_time, "to_time": to_time, "from_field": "", "to_field": "", "limit_from": "", "limit_to": "",
        "density_threshold": 0, "ticks": [], "case_url": case_url,
        **_page_context(profile),
    }
    try:
        mod = _load_module()
    except ImportError as exc:
        ctx["error"] = f"The case module is not available: {exc}"
        return templates.TemplateResponse(request, "case_detail.html", ctx)

    ctx["density_threshold"] = mod.DENSITY_THRESHOLD
    try:
        with deps.open_database(profile) as db:
            v = mod.load_case(db, profile, key)
    except Exception as exc:  # noqa: BLE001
        ctx["error"] = config.redact(str(exc))
        return templates.TemplateResponse(request, "case_detail.html", ctx)

    window = _parse_window(from_time, to_time, v, profile)
    rows, bands, used = build_timeline(v, window=window)
    ctx.update({
        "case": v, "rows": rows, "gap_bands": bands, "window": used,
        "density": build_density(v, profile) if len(v.trees) > mod.DENSITY_THRESHOLD else [],
        "instances_page": _paginate(v.instances, ctx["page"]),
        "ticks": build_ticks(used[0], used[1], profile) if used else [],
        # The calendar is clamped to the actual period of the case -- so it is not possible to
        # zoom into a range where nothing happened.
        "from_field": _field_value(from_time, profile) or _display(v.window_start, profile, FIELD_FORMAT),
        "to_field": _field_value(to_time, profile) or _display(v.window_end, profile, FIELD_FORMAT),
        "limit_from": _display(v.window_start, profile, FIELD_FORMAT),
        "limit_to": _display(v.window_end, profile, FIELD_FORMAT),
        "suggestions": _zoom_suggestions(v, profile),
        "gap_rows": [g for g in v.gaps if g.kind is GapKind.BETWEEN],
        "parallel_count": sum(1 for g in v.gaps if g.kind is GapKind.OVERLAP),
    })
    return templates.TemplateResponse(request, "case_detail.html", ctx)


def _zoom_suggestions(v: Case, profile: Profile) -> list[tuple[str, str, str]]:
    """A few windows that a timeline always wants: (label, from_time, to_time).

    They save typing without asserting anything -- the whole case stays one click away, and the
    bounds are visible in the fields.
    """
    if not v.window_start or not v.window_end:
        return []
    span = v.window_end - v.window_start
    options: list[tuple[str, str, str]] = []
    if span > timedelta(hours=2):
        options.append(("first hour", _display(v.window_start, profile, FIELD_FORMAT),
                    _display(v.window_start + timedelta(hours=1), profile, FIELD_FORMAT)))
    if span > timedelta(days=1):
        options.append(("first day", _display(v.window_start, profile, FIELD_FORMAT),
                    _display(v.window_start + timedelta(days=1), profile, FIELD_FORMAT)))
        options.append(("last day", _display(v.window_end - timedelta(days=1), profile, FIELD_FORMAT),
                    _display(v.window_end, profile, FIELD_FORMAT)))
    if span > timedelta(days=30):
        options.append(("last 30 days", _display(v.window_end - timedelta(days=30), profile, FIELD_FORMAT),
                    _display(v.window_end, profile, FIELD_FORMAT)))
    return options


def _parse_window(from_time: str, to_time: str, v: Case,
                  profile: Profile) -> tuple[datetime, datetime] | None:
    """The inputs are in the display zone -- that is what the page shows -- and are converted to
    database time before being compared with instance timestamps."""
    def parse(text: str) -> datetime | None:
        text = (text or "").strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    a, b = parse(from_time), parse(to_time)
    if a is None and b is None:
        return None
    a = from_display_tz(a, profile) if a else None
    b = from_display_tz(b, profile) if b else None
    start = a or v.window_start
    end = b or v.window_end
    if not start or not end:
        return None
    return (start, end)


def _paginate(instances, page: int):
    total = len(instances)
    pages = max(1, (total + TABLE_PAGE - 1) // TABLE_PAGE)
    page = min(max(1, page), pages)
    lo = (page - 1) * TABLE_PAGE
    return {"rows": instances[lo:lo + TABLE_PAGE], "page": page, "pages": pages,
            "total": total, "from_time": lo + 1, "to_time": min(total, lo + TABLE_PAGE)}
