"""Read BPMN models and draw them as SVG.

Camunda 7 stores the BPMN file of every deployed definition version in ``act_ge_bytearray``,
including diagram coordinates (``bpmndi:BPMNShape``, ``dc:Bounds``). That is enough to render
the model here, without a third-party library and without a megabyte of JavaScript -- so the
instance page stays readable with scripting disabled.

Three details decide whether the result is useful or merely present:

* **External labels.** Events and gateways do not carry their name inside the shape but in a
  separate ``bpmndi:BPMNLabel`` with its own coordinates. Ignore that and you render a diagram
  in which none of the events is named -- which can easily be most of the labels in a model.
* **Wrapping.** Task names are longer than the boxes that hold them. Without wrapping, half of
  every name sits outside its box.
* **Type markers.** Without the gear, the person, the envelope and the differing stroke widths,
  a service task, a user task, a call activity and an event all look the same, and the picture
  stops carrying information.

**What this deliberately does not claim:** the engine does not record sequence flows in its
history. What gets highlighted is therefore the set of *activities* that ran. Two highlighted
nodes do not imply that the edge between them was taken, and the drawing does not suggest
otherwise.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import escape
from typing import Iterable

NS_MODEL = "http://www.omg.org/spec/BPMN/20100524/MODEL"

_EVENTS = {"startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent",
           "boundaryEvent"}
_GATEWAYS = {"exclusiveGateway", "parallelGateway", "inclusiveGateway", "eventBasedGateway",
             "complexGateway"}
_CONTAINERS = {"participant", "lane", "subProcess", "transaction", "adHocSubProcess"}

#: Small glyphs per activity type, drawn relative to the top-left corner of the shape.
_TASK_MARKER = {
    "serviceTask": "gear",
    "sendTask": "envelope",
    "receiveTask": "envelope",
    "userTask": "person",
    "businessRuleTask": "table",
    "scriptTask": "script",
    "manualTask": "hand",
    "callActivity": "call",
}

_EVENT_GLYPH = {
    "message": "envelope", "timer": "clock", "error": "bolt", "signal": "triangle",
    "escalation": "arrow", "compensation": "rewind", "conditional": "list",
    "terminate": "filled", "link": "arrow", "cancel": "cross",
}


@dataclass(frozen=True)
class Shape:
    element_id: str
    kind: str
    name: str | None
    x: float
    y: float
    width: float
    height: float
    expanded: bool = True
    event_kind: str | None = None          # error, timer, message, signal, ...
    throwing: bool = False                 # filled symbol (throw) instead of outline (catch)
    multi_instance: bool = False
    label: tuple[float, float, float, float] | None = None   # own label coordinates


@dataclass(frozen=True)
class Edge:
    element_id: str
    points: tuple[tuple[float, float], ...]
    name: str | None = None


@dataclass(frozen=True)
class Diagram:
    shapes: tuple[Shape, ...] = ()
    edges: tuple[Edge, ...] = ()
    process_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def has_layout(self) -> bool:
        return bool(self.shapes)

    def bounds(self) -> tuple[float, float, float, float]:
        if not self.shapes and not self.edges:
            return 0.0, 0.0, 100.0, 100.0
        xs, ys, xe, ye = [], [], [], []
        for s in self.shapes:
            xs.append(s.x); ys.append(s.y); xe.append(s.x + s.width); ye.append(s.y + s.height)
            if s.label:
                lx, ly, lw, lh = s.label
                xs.append(lx); ys.append(ly); xe.append(lx + lw); ye.append(ly + lh)
        for e in self.edges:
            for x, y in e.points:
                xs.append(x); ys.append(y); xe.append(x); ye.append(y)
        return min(xs), min(ys), max(xe), max(ye)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _event_kind(el: ET.Element) -> tuple[str | None, bool]:
    """Event kind from the child elements (``errorEventDefinition`` etc.) and whether it throws."""
    for child in el:
        name = _local(child.tag)
        if name.endswith("EventDefinition"):
            kind = name[: -len("EventDefinition")]
            throwing = _local(el.tag) in ("intermediateThrowEvent", "endEvent")
            return kind, throwing
    if _local(el.tag) in ("intermediateThrowEvent", "endEvent"):
        return None, True
    return None, False


def parse(xml_bytes: bytes | str) -> Diagram:
    """Read a BPMN file.

    Errors are collected rather than raised: an unreadable model must not take the whole
    instance page down with it.
    """
    try:
        root = ET.fromstring(xml_bytes if isinstance(xml_bytes, bytes) else xml_bytes.encode())
    except ET.ParseError as exc:
        return Diagram(errors=(f"BPMN XML could not be parsed: {exc}",))

    errors: list[str] = []
    info: dict[str, dict[str, object]] = {}
    for el in root.iter():
        eid = el.get("id")
        tag = _local(el.tag)
        if not eid or tag in ("BPMNShape", "BPMNEdge", "BPMNDiagram", "BPMNPlane", "BPMNLabel"):
            continue
        kind, throwing = _event_kind(el) if tag in _EVENTS else (None, False)
        multi = any(_local(c.tag) == "multiInstanceLoopCharacteristics" for c in el)
        info[eid] = {"kind": tag, "name": el.get("name"), "event_kind": kind,
                     "throwing": throwing, "multi_instance": multi}

    process_ids = tuple(el.get("id") for el in root.iter()
                        if _local(el.tag) == "process" and el.get("id"))

    shapes: list[Shape] = []
    edges: list[Edge] = []
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "BPMNShape":
            ref = el.get("bpmnElement") or ""
            bounds = next((c for c in el if _local(c.tag) == "Bounds"), None)
            if bounds is None:
                continue
            label_bounds = None
            for c in el:
                if _local(c.tag) == "BPMNLabel":
                    lb = next((g for g in c if _local(g.tag) == "Bounds"), None)
                    if lb is not None:
                        try:
                            label_bounds = (float(lb.get("x", 0)), float(lb.get("y", 0)),
                                            float(lb.get("width", 0)), float(lb.get("height", 0)))
                        except ValueError:
                            pass
            meta = info.get(ref, {"kind": "unknown", "name": None})
            try:
                shapes.append(Shape(
                    element_id=ref, kind=str(meta.get("kind")), name=meta.get("name"),  # type: ignore[arg-type]
                    x=float(bounds.get("x", 0)), y=float(bounds.get("y", 0)),
                    width=float(bounds.get("width", 0)), height=float(bounds.get("height", 0)),
                    expanded=el.get("isExpanded", "true") != "false",
                    event_kind=meta.get("event_kind"),  # type: ignore[arg-type]
                    throwing=bool(meta.get("throwing")),
                    multi_instance=bool(meta.get("multi_instance")),
                    label=label_bounds))
            except ValueError:
                errors.append(f"invalid coordinates at {ref}")
        elif tag == "BPMNEdge":
            pts = []
            for c in el:
                if _local(c.tag) == "waypoint":
                    try:
                        pts.append((float(c.get("x", 0)), float(c.get("y", 0))))
                    except ValueError:
                        pass
            if len(pts) >= 2:
                ref = el.get("bpmnElement") or ""
                edges.append(Edge(element_id=ref, points=tuple(pts),
                                  name=(info.get(ref) or {}).get("name")))  # type: ignore[arg-type]

    shapes.sort(key=lambda s: (0 if s.kind in _CONTAINERS else 1, -s.width * s.height))
    return Diagram(shapes=tuple(shapes), edges=tuple(edges), process_ids=process_ids,
                   errors=tuple(errors))


# --- Drawing ------------------------------------------------------------------------------

def _wrap(text: str, width: float, char_width: float = 5.3) -> list[str]:
    """Wrap a name onto the box width. Without this, half of it sits outside the box."""
    max_chars = max(6, int((width - 8) / char_width))
    rows: list[str] = []
    current = ""
    for word in text.split():
        candidate = (current + " " + word).strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                rows.append(current)
            current = word if len(word) <= max_chars else word[: max_chars - 1] + "…"
        if len(rows) == 3:
            break
    if current and len(rows) < 4:
        rows.append(current)
    return rows[:4]


def _text_block(text: str, cx: float, cy: float, width: float, *, css_class: str = "bpmn-text",
                anchor: str = "middle") -> str:
    rows = _wrap(text, width)
    if not rows:
        return ""
    row_height = 11.0
    start = cy - (len(rows) - 1) * row_height / 2
    parts = [f'<text class="{css_class}" x="{cx:.0f}" y="{start:.0f}" text-anchor="{anchor}">']
    for i, row in enumerate(rows):
        dy = 0 if i == 0 else row_height
        parts.append(f'<tspan x="{cx:.0f}" dy="{dy:.0f}">{escape(row)}</tspan>')
    parts.append("</text>")
    return "".join(parts)


def _marker(kind: str, x: float, y: float) -> str:
    """A small glyph, top-left inside an activity or centred inside an event."""
    g = f'<g class="bpmn-marker" transform="translate({x:.0f},{y:.0f})">'
    if kind == "gear":
        g += ('<circle cx="6" cy="6" r="4.2" class="bpmn-glyph-stroke"/>'
              '<circle cx="6" cy="6" r="1.6" class="bpmn-glyph-stroke"/>'
              '<path d="M6 0.6V2M6 10V11.4M0.6 6H2M10 6H11.4M2.2 2.2l1 1M9.8 9.8l-1-1M9.8 2.2l-1 1M2.2 9.8l1-1" class="bpmn-glyph-stroke"/>')
    elif kind == "envelope":
        g += ('<rect x="1" y="2.5" width="10" height="7" class="bpmn-glyph-stroke"/>'
              '<path d="M1 2.5l5 4 5-4" class="bpmn-glyph-stroke"/>')
    elif kind == "person":
        g += ('<circle cx="6" cy="3.6" r="2.2" class="bpmn-glyph-stroke"/>'
              '<path d="M1.8 11c0-2.6 1.9-4.2 4.2-4.2S10.2 8.4 10.2 11" class="bpmn-glyph-stroke"/>')
    elif kind == "table":
        g += ('<rect x="1" y="2" width="10" height="8" class="bpmn-glyph-stroke"/>'
              '<path d="M1 4.6h10M4.4 2v8" class="bpmn-glyph-stroke"/>')
    elif kind == "script":
        g += ('<path d="M3 2c2 0 2 8 4 8M3 2h5M5 10h5" class="bpmn-glyph-stroke"/>')
    elif kind == "hand":
        g += '<path d="M3 10V5a1 1 0 012 0V3a1 1 0 012 0v2a1 1 0 012 0v5" class="bpmn-glyph-stroke"/>'
    elif kind == "call":
        g += ('<rect x="1" y="2" width="10" height="8" class="bpmn-glyph-stroke"/>'
              '<path d="M6 4v4M4 6h4" class="bpmn-glyph-stroke"/>')
    elif kind == "clock":
        g += ('<circle cx="6" cy="6" r="5" class="bpmn-glyph-stroke"/>'
              '<path d="M6 2.5V6l2.5 1.6" class="bpmn-glyph-stroke"/>')
    elif kind == "bolt":
        g += '<path d="M7.5 1L2.5 7h3l-1 4 5-6.2h-3z" class="bpmn-glyph-stroke"/>'
    elif kind == "triangle":
        g += '<path d="M6 1.5L11 10.5H1z" class="bpmn-glyph-stroke"/>'
    elif kind == "arrow":
        g += '<path d="M2 6h7M6.5 3l3 3-3 3" class="bpmn-glyph-stroke"/>'
    elif kind == "rewind":
        g += '<path d="M6 2L1.5 6 6 10zM11 2L6.5 6 11 10z" class="bpmn-glyph-stroke"/>'
    elif kind == "list":
        g += ('<rect x="1.5" y="1.5" width="9" height="9" class="bpmn-glyph-stroke"/>'
              '<path d="M3.5 4h5M3.5 6h5M3.5 8h3" class="bpmn-glyph-stroke"/>')
    elif kind == "cross":
        g += '<path d="M2 2l8 8M10 2l-8 8" class="bpmn-glyph-stroke"/>'
    elif kind == "filled":
        g += '<circle cx="6" cy="6" r="4.5" class="bpmn-glyph-filled"/>'
    elif kind == "multi":
        g += '<path d="M2 2v8M6 2v8M10 2v8" class="bpmn-glyph-stroke"/>'
    g += "</g>"
    return g


def to_svg(diagram: Diagram, *, visited: Iterable[str] = (), current: Iterable[str] = (),
           scale: float = 1.0) -> str:
    """Draw the diagram.

    ``visited`` are the activity ids that ran, ``current`` those still open at query time.

    **Drawn at 1:1**, not squeezed into the page width. Real models are a couple of thousand
    units wide; scaled into a 1000 pixel column their labels end up around five pixels tall and
    the picture is worthless. So the SVG carries its natural size and its frame scrolls -- which
    stays readable, and an overview remains available by asking for a smaller scale explicitly.
    """
    if not diagram.has_layout:
        return ""
    visited_ids = {v for v in visited if v}
    open_ids = {c for c in current if c}
    x0, y0, x1, y1 = diagram.bounds()
    pad = 24
    w = max(1.0, x1 - x0 + 2 * pad)
    h = max(1.0, y1 - y0 + 2 * pad)

    parts: list[str] = [
        f'<svg class="bpmn" viewBox="{x0 - pad:.0f} {y0 - pad:.0f} {w:.0f} {h:.0f}" '
        f'width="{w * scale:.0f}" height="{h * scale:.0f}" role="img" '
        f'aria-label="BPMN model, executed activities highlighted">',
        '<defs><marker id="bpmn-arrowhead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" '
        'markerHeight="5" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" class="bpmn-arrow"/></marker></defs>',
    ]

    for edge in diagram.edges:
        pts = " ".join(f"{x:.0f},{y:.0f}" for x, y in edge.points)
        parts.append(
            f'<polyline class="bpmn-edge" points="{pts}" marker-end="url(#bpmn-arrowhead)"/>')

    for s in diagram.shapes:
        state = ("is-current" if s.element_id in open_ids
                 else "is-visited" if s.element_id in visited_ids else "")
        css_class = " ".join(x for x in ["bpmn-shape", f"bpmn-{s.kind}", state,
                                     "is-container" if s.kind in _CONTAINERS else ""] if x)
        title = escape(f"{s.name or s.element_id} · {s.kind}"
                       + (f" ({s.event_kind})" if s.event_kind else "")
                       + (" · executed" if s.element_id in visited_ids else "")
                       + (" · still open" if s.element_id in open_ids else ""))
        cx, cy = s.x + s.width / 2, s.y + s.height / 2

        if s.kind in _EVENTS:
            r = max(6.0, min(s.width, s.height) / 2)
            thick = "bpmn-end" if s.kind == "endEvent" else ""
            parts.append(f'<circle class="{css_class} {thick}" cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}">'
                         f'<title>{title}</title></circle>')
            if s.kind in ("intermediateCatchEvent", "intermediateThrowEvent", "boundaryEvent"):
                parts.append(f'<circle class="bpmn-ring" cx="{cx:.0f}" cy="{cy:.0f}" r="{r - 3:.0f}"/>')
            glyph = _EVENT_GLYPH.get(s.event_kind or "")
            if glyph:
                parts.append(_marker(glyph, cx - 6, cy - 6))
        elif s.kind in _GATEWAYS:
            pts = (f"{cx:.0f},{s.y:.0f} {s.x + s.width:.0f},{cy:.0f} "
                   f"{cx:.0f},{s.y + s.height:.0f} {s.x:.0f},{cy:.0f}")
            parts.append(f'<polygon class="{css_class}" points="{pts}"><title>{title}</title></polygon>')
            symbol = {"exclusiveGateway": "M-5-5L5 5M5-5L-5 5",
                      "parallelGateway": "M0-6V6M-6 0H6",
                      "inclusiveGateway": ""}.get(s.kind, "")
            if s.kind == "inclusiveGateway":
                parts.append(f'<circle class="bpmn-glyph-stroke" cx="{cx:.0f}" cy="{cy:.0f}" r="6"/>')
            elif symbol:
                parts.append(f'<path class="bpmn-glyph-stroke" transform="translate({cx:.0f},{cy:.0f})" d="{symbol}"/>')
        else:
            parts.append(
                f'<rect class="{css_class}" x="{s.x:.0f}" y="{s.y:.0f}" width="{s.width:.0f}" '
                f'height="{s.height:.0f}" rx="6"><title>{title}</title></rect>')
            if s.kind == "callActivity":
                parts.append(f'<rect class="bpmn-call-frame" x="{s.x + 2:.0f}" y="{s.y + 2:.0f}" '
                             f'width="{s.width - 4:.0f}" height="{s.height - 4:.0f}" rx="5"/>')
            marker = _TASK_MARKER.get(s.kind)
            if marker and s.width > 40:
                parts.append(_marker(marker, s.x + 4, s.y + 4))
            if s.multi_instance and s.width > 40:
                parts.append(_marker("multi", cx - 6, s.y + s.height - 14))
            if not s.expanded and s.kind in _CONTAINERS:
                parts.append(_marker("call", cx - 6, s.y + s.height - 14))

        # Label: use the model's own coordinates when it provides them (events, gateways),
        # otherwise wrap it into the shape.
        if s.name:
            if s.label:
                lx, ly, lw, lh = s.label
                parts.append(_text_block(s.name, lx + lw / 2, ly + lh / 2 + 3, max(lw, 60),
                                         css_class="bpmn-text bpmn-text-external"))
            elif s.kind in _CONTAINERS:
                parts.append(f'<text class="bpmn-text bpmn-container-text" x="{s.x + 8:.0f}" '
                             f'y="{s.y + 15:.0f}">{escape(s.name[:70])}</text>')
            elif s.kind in _EVENTS or s.kind in _GATEWAYS:
                parts.append(_text_block(s.name, cx, s.y + s.height + 12, 110,
                                         css_class="bpmn-text bpmn-text-external"))
            elif s.width >= 40:
                parts.append(_text_block(s.name, cx, cy + 4, s.width))

    parts.append("</svg>")
    return "\n".join(parts)
