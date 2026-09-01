"""The mark list in the interface.

A button on every case and every instance, a list with notes, export as JSON and CSV. The list
never contains variable values -- only references and the user's own text.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from starlette.templating import Jinja2Templates

from .. import config
from ..notes import MarkKind, Notes, default_notes
from . import deps
from .views_definitions import fmt_duration_ms, fmt_fraction, fmt_pct
from .views_cases import case_url
from .views_instance import instance_url

log = logging.getLogger("cib7explorer.web.views_marks")

router = APIRouter()
_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
for _name, _fn in (("num", deps.fmt_int), ("bytes_size", deps.fmt_bytes),
                   ("datetime", deps.fmt_dt), ("ago", deps.fmt_ago),
                   ("pct", fmt_pct), ("fraction", fmt_fraction),
                   ("duration", fmt_duration_ms)):
    templates.env.filters[_name] = _fn
templates.env.globals["static_version"] = deps.static_version
templates.env.globals["base_path"] = deps.base_path
templates.env.globals["auth_enabled"] = deps.auth_enabled


def _notes() -> Notes:
    return default_notes()


@router.get("/marks", response_class=HTMLResponse)
def marks(request: Request, kind: str = "", profile: str = "") -> HTMLResponse:
    notes = _notes()
    entries = notes.all(kind=kind or None, profile_name=profile or None)
    return templates.TemplateResponse(request, "marks.html", {
        "request": request, "marks": entries, "kind": kind,
        # The selected filter and the list of available profiles are two different things and
        # must not share a key -- one silently overwrote the other once.
        "profile_filter": profile,
        "profile_names": sorted(deps.load_profiles()),
        "profiles": deps.load_profiles(),
        "total": notes.count(), "path": str(notes.path), "kinds": list(MarkKind),
        "case_url": case_url, "instance_url": instance_url,
    })


@router.post("/marks/new", response_class=HTMLResponse)
def add_mark(request: Request, kind: str = Form(...), reference: str = Form(...),
             note: str = Form(""), profile: str = Form(""),
             context: str = Form("")) -> HTMLResponse:
    """Create a mark and return the fragment for the place the mark was made from."""
    notes = _notes()
    try:
        mark = notes.add(kind, reference, note, profile_name=profile, context=context)
        error = None
    except Exception as exc:  # noqa: BLE001 -- a failed mark must not take the page down
        mark, error = None, config.redact(str(exc))
    return templates.TemplateResponse(request, "fragments/mark_button.html", {
        "request": request, "kind": kind, "reference": reference, "profile": profile,
        "context": context, "marks": notes.for_reference(kind, reference),
        "just_marked": mark, "error": error,
    })


@router.get("/marks/button", response_class=HTMLResponse)
def mark_button(request: Request, kind: str, reference: str, profile: str = "",
                context: str = "") -> HTMLResponse:
    notes = _notes()
    return templates.TemplateResponse(request, "fragments/mark_button.html", {
        "request": request, "kind": kind, "reference": reference, "profile": profile,
        "context": context, "marks": notes.for_reference(kind, reference),
        "just_marked": None, "error": None,
    })


@router.post("/marks/{mark_id}/delete", response_class=HTMLResponse)
def delete(request: Request, mark_id: int) -> HTMLResponse:
    _notes().remove(mark_id)
    return HTMLResponse("")


@router.post("/marks/{mark_id}/note", response_class=HTMLResponse)
def update_note(request: Request, mark_id: int, note: str = Form("")) -> HTMLResponse:
    notes = _notes()
    notes.update_note(mark_id, note)
    mark = next((m for m in notes.all() if m.id == mark_id), None)
    return templates.TemplateResponse(request, "fragments/mark_row.html", {
        "request": request, "m": mark, "case_url": case_url, "instance_url": instance_url,
        "profiles": deps.load_profiles(),
    })


@router.get("/marks.json")
def export_json() -> Response:
    return Response(_notes().to_json(), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="marks.json"'})


@router.get("/marks.csv")
def export_csv() -> Response:
    return Response(_notes().to_csv(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="marks.csv"'})
