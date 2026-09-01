"""Zooming into one process instance.

The sequence of activities, the user tasks with whatever the history holds, the version-exact
BPMN model with the activities that actually ran highlighted -- and the variables, subject to the
value policy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.templating import Jinja2Templates

from .. import bpmn, config
from ..config import Profile
from ..contracts import Classification
from ..values import REQUEST_MAX_BYTES, resolve_access
from . import deps
from .views_definitions import fmt_duration_ms, fmt_fraction, fmt_pct
from .views_cases import case_url

log = logging.getLogger("cib7explorer.web.views_instance")

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


def instance_url(profile_name: str, proc_inst_id: str) -> str:
    from urllib.parse import quote

    return deps.with_base_path(f"/profile/{quote(profile_name)}/instance/{quote(proc_inst_id, safe='')}")


def _base_ctx(profile: Profile) -> dict[str, Any]:
    prod_warning = None
    if profile.classification is Classification.PROD:
        prod_warning = ("this connection points at a database classified as PROD. Variable "
                        "values appear only as far as the allowlist permits.")
    return {"prod_warning": prod_warning, "instance_url": instance_url,
            "case_url": case_url}


@router.get("/profile/{name}/instance/{proc_inst_id}", response_class=HTMLResponse)
def instance_detail(request: Request, name: str, proc_inst_id: str,
                   model: int = 1) -> HTMLResponse:
    profile = deps.get_profile_or_404(name)
    ctx: dict[str, Any] = {
        "profile": profile, "request": request, "proc_inst_id": proc_inst_id,
        "detail": None, "svg": "", "diagram_hint": "", "error": None,
        "model": bool(model), **_base_ctx(profile),
    }
    try:
        from ..db import detect as detect_mod, instance as mod
    except ImportError as exc:
        ctx["error"] = f"The instance module is not available: {exc}"
        return templates.TemplateResponse(request, "instance_detail.html", ctx)

    try:
        with deps.open_database(profile) as db:
            det = mod.load_instance(db, profile, proc_inst_id)
            if det is None:
                ctx["error"] = (f"There is no entry for instance id '{proc_inst_id}' in this "
                                "history. It is also possible that history cleanup removed it.")
                return templates.TemplateResponse(request, "instance_detail.html", ctx)
            ctx["detail"] = det
            if model and det.instance.def_id:
                xml = mod.load_bpmn_xml(db, det.instance.def_id)
                if xml:
                    diagram = bpmn.parse(xml)
                    if diagram.has_layout:
                        ctx["svg"] = bpmn.to_svg(diagram, visited=det.visited_act_ids,
                                                 current=det.open_act_ids)
                        ctx["diagram_hint"] = (
                            f"{len(diagram.shapes)} elements from {det.bpmn_resource}, "
                            f"version {det.definition_version} — exactly the version this "
                            "instance ran, not the newest one.")
                    else:
                        ctx["diagram_hint"] = (
                            "The BPMN carries no diagram coordinates; it can be downloaded but "
                            "not drawn."
                            + (" " + "; ".join(diagram.errors) if diagram.errors else ""))
                else:
                    ctx["diagram_hint"] = "The BPMN resource cannot be found."
    except Exception as exc:  # noqa: BLE001
        ctx["error"] = config.redact(str(exc))
    return templates.TemplateResponse(request, "instance_detail.html", ctx)


@router.get("/profile/{name}/instance/{proc_inst_id}/bpmn")
def instance_bpmn(name: str, proc_inst_id: str) -> Response:
    """The version-exact model for download -- the version has to match the instance."""
    profile = deps.get_profile_or_404(name)
    from ..db import instance as mod

    with deps.open_database(profile) as db:
        det = mod.load_instance(db, profile, proc_inst_id)
        if det is None or not det.instance.def_id:
            return Response("Instance not found.", status_code=404, media_type="text/plain")
        xml = mod.load_bpmn_xml(db, det.instance.def_id)
    if xml is None:
        return Response("BPMN resource not found.", status_code=404, media_type="text/plain")
    filename = f"{det.instance.def_key}-v{det.definition_version}.bpmn"
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/profile/{name}/instance/{proc_inst_id}/value.json")
def instance_value_json(name: str, proc_inst_id: str, variable: str) -> JSONResponse:
    """One variable value in full, for the value dialog opened by a click.

    The table shows only a preview: a few thousand characters of JSON in a table cell are useless,
    and with dozens of variables the page would be unreadable. Only what somebody actually looks
    at gets fetched -- which is also what makes "large values are never loaded automatically"
    true.
    """
    profile = deps.get_profile_or_404(name)
    try:
        from ..db import instance as mod

        with deps.open_database(profile) as db:
            result = mod.load_value(db, profile, proc_inst_id, variable)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"allowed": False, "reason": config.redact(str(exc))}, status_code=200)
    return JSONResponse(result)


@router.get("/profile/{name}/instance/{proc_inst_id}/value", response_class=HTMLResponse)
def instance_value(request: Request, name: str, proc_inst_id: str, variable: str = "",
                 bytearray: str = "", def_key: str = "") -> HTMLResponse:
    """Load a large or binary value on explicit request."""
    profile = deps.get_profile_or_404(name)
    #: Called directly -- JavaScript off, or the link opened in a new tab -- the page renders
    #: with a heading and a way back; via HTMX it stays a fragment.
    via_htmx = request.headers.get("hx-request") == "true"
    ctx: dict[str, Any] = {"profile": profile, "request": request, "variable": variable,
                           "value": None, "size": None, "hint": "", "error": None,
                           "limit": REQUEST_MAX_BYTES, "page": not via_htmx,
                           "back": instance_url(name, proc_inst_id) + "#variables",
                           **_base_ctx(profile)}
    access = resolve_access(profile)
    if not access.allows(def_key or None, variable):
        ctx["error"] = access.why_not(def_key or None, variable)
        return templates.TemplateResponse(request, "fragments/instance_value.html", ctx)
    try:
        from ..db import instance as mod

        with deps.open_database(profile) as db:
            # Through load_value, so that this page shows the same value as the dialog --
            # whether it is inline, in a bytearray, or not resolvable as text at all.
            result = mod.load_value(db, profile, proc_inst_id, variable)
        ctx.update({"value": result.get("value") or result.get("raw_bytes"),
                    "size": result.get("size"),
                    "hint": result.get("hint", ""),
                    "error": None if result.get("allowed") else result.get("reason")})
    except Exception as exc:  # noqa: BLE001
        ctx["error"] = config.redact(str(exc))
    return templates.TemplateResponse(request, "fragments/instance_value.html", ctx)
