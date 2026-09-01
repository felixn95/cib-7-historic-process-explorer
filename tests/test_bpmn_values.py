"""Tests for the BPMN renderer and the value policy."""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from cib7explorer import bpmn, config, values
from cib7explorer.db import instance as instance_db
from cib7explorer.contracts import Classification

MINI = """<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
  xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"
  xmlns:dc="http://www.omg.org/spec/DD/20100524/DC"
  xmlns:di="http://www.omg.org/spec/DD/20100524/DI" id="d1">
  <bpmn:process id="p1">
    <bpmn:startEvent id="S1" name="Start"/>
    <bpmn:serviceTask id="T1" name="does something"/>
    <bpmn:exclusiveGateway id="G1" name="or"/>
    <bpmn:endEvent id="E1" name="End"/>
    <bpmn:sequenceFlow id="F1" sourceRef="S1" targetRef="T1"/>
  </bpmn:process>
  <bpmndi:BPMNDiagram id="dia"><bpmndi:BPMNPlane id="plane" bpmnElement="p1">
    <bpmndi:BPMNShape id="s_S1" bpmnElement="S1"><dc:Bounds x="100" y="100" width="36" height="36"/></bpmndi:BPMNShape>
    <bpmndi:BPMNShape id="s_T1" bpmnElement="T1"><dc:Bounds x="200" y="80" width="100" height="80"/></bpmndi:BPMNShape>
    <bpmndi:BPMNShape id="s_G1" bpmnElement="G1"><dc:Bounds x="350" y="93" width="50" height="50"/></bpmndi:BPMNShape>
    <bpmndi:BPMNShape id="s_E1" bpmnElement="E1"><dc:Bounds x="450" y="100" width="36" height="36"/></bpmndi:BPMNShape>
    <bpmndi:BPMNEdge id="e_F1" bpmnElement="F1">
      <di:waypoint x="136" y="118"/><di:waypoint x="200" y="118"/>
    </bpmndi:BPMNEdge>
  </bpmndi:BPMNPlane></bpmndi:BPMNDiagram>
</bpmn:definitions>"""


# --- BPMN -------------------------------------------------------------------------------

def test_parsing_yields_shapes_types_and_edges():
    d = bpmn.parse(MINI)
    assert d.has_layout
    assert len(d.shapes) == 4
    assert len(d.edges) == 1
    assert d.process_ids == ("p1",)
    typen = {s.element_id: s.kind for s in d.shapes}
    assert typen == {"S1": "startEvent", "T1": "serviceTask", "G1": "exclusiveGateway",
                     "E1": "endEvent"}
    assert {s.element_id: s.name for s in d.shapes}["T1"] == "does something"


def test_unreadable_bpmn_does_not_raise():
    d = bpmn.parse("<this is not xml")
    assert not d.has_layout
    assert d.errors


def test_missing_diagram_coordinates_are_detected():
    without_di = MINI[:MINI.index("<bpmndi:BPMNDiagram")] + "</bpmn:definitions>"
    d = bpmn.parse(without_di)
    assert not d.has_layout, "without a BPMNShape there is nothing to draw"


def test_svg_draws_shapes_according_to_their_type():
    """What is counted are the classified shapes, not every SVG element: the type markers (gear,
    person) are themselves made of circles and paths."""
    import re

    svg = bpmn.to_svg(bpmn.parse(MINI))
    formen = re.findall(r'<(circle|polygon|rect) class="(bpmn-shape[^"]*)"', svg)
    kinds = {}
    for tag, classes in formen:
        for k in classes.split():
            if k.startswith("bpmn-") and k != "bpmn-shape":
                kinds[k] = tag
    assert kinds.get("bpmn-startEvent") == "circle"
    assert kinds.get("bpmn-endEvent") == "circle"
    assert kinds.get("bpmn-exclusiveGateway") == "polygon", "a gateway should be a diamond"
    assert kinds.get("bpmn-serviceTask") == "rect", "a service task should be a rectangle"
    assert "<polyline" in svg, "a sequence flow should be a line"


def test_svg_marks_only_visited_activities_and_no_edges():
    """The engine does not record sequence flows. A highlighted edge would be a claim."""
    svg = bpmn.to_svg(bpmn.parse(MINI), visited={"S1", "T1"}, current={"G1"})
    assert svg.count("is-visited") == 2
    assert svg.count("is-current") == 1
    kanten = [row for row in svg.splitlines() if "bpmn-edge" in row]
    assert kanten and not any("is-visited" in z for z in kanten)


def test_svg_stays_empty_without_a_layout():
    assert bpmn.to_svg(bpmn.parse("<x/>")) == ""


@pytest.mark.integration
def test_a_real_model_from_the_database(db):
    from cib7explorer.db import instance

    row = db.fetch("SELECT proc_def_id_ FROM act_hi_procinst LIMIT 1", limit=1)
    xml = instance_db.load_bpmn_xml(db, row.rows[0][0])
    assert xml and "BPMNShape" in xml
    d = bpmn.parse(xml)
    assert d.has_layout and len(d.shapes) > 5
    assert not d.errors


# --- Value mode -------------------------------------------------------------------------

def _profile_of(**kw) -> config.Profile:
    base_path = dict(name="t", classification=Classification.TEST)
    base_path.update(kw)
    return config.Profile(**base_path)


def test_test_profile_without_a_list_shows_everything():
    a = values.resolve_access(_profile_of())
    assert a.policy is values.ValuePolicy.ALL
    assert a.allows("anything", "anyhow")


def test_prod_profile_without_a_list_shows_nothing():
    a = values.resolve_access(_profile_of(classification=Classification.PROD))
    assert a.policy is values.ValuePolicy.NONE
    assert not a.allows("order-8000", "orderNumber")
    assert "prod" in a.reason or "allowlist" in a.reason


def test_unknown_classification_shows_nothing():
    a = values.resolve_access(_profile_of(classification=Classification.UNKNOWN))
    assert a.policy is values.ValuePolicy.NONE


def test_allowlist_with_patterns():
    d = pathlib.Path(tempfile.mkdtemp()) / "allow.yaml"
    values.write_example_allowlist(d, [("order-*", ["orderNumber", "unitNumber"]),
                                       ("quote-2000", ["*Number"])])
    a = values.resolve_access(_profile_of(classification=Classification.PROD,
                                      values_mode=True, values_allowlist_file=str(d)))
    assert a.policy is values.ValuePolicy.ALLOWLIST
    assert a.allows("order-8000", "orderNumber")
    assert not a.allows("order-8000", "secretField")
    assert a.allows("quote-2000", "ticketNumber")
    assert not a.allows("ticket-1000", "orderNumber")
    assert "not in the allowlist" in a.why_not("order-8000", "secretField")


def test_an_unreadable_allowlist_closes_the_gate():
    a = values.resolve_access(_profile_of(values_mode=True,
                                      values_allowlist_file="/does/not/exist.yaml"))
    assert a.policy is values.ValuePolicy.NONE
    assert "cannot be read" in a.reason


def test_the_size_limits_are_set():
    assert values.AUTO_LOAD_MAX_BYTES < values.REQUEST_MAX_BYTES
    assert values.AUTO_LOAD_MAX_BYTES <= 8192, "large values are never loaded automatically"


# --- drawing: what a first attempt gets wrong -----------------------------------------------

WITH_EXTERNAL_LABEL = MINI.replace(
    '<bpmndi:BPMNShape id="s_S1" bpmnElement="S1"><dc:Bounds x="100" y="100" width="36" height="36"/></bpmndi:BPMNShape>',
    '<bpmndi:BPMNShape id="s_S1" bpmnElement="S1"><dc:Bounds x="100" y="100" width="36" height="36"/>'
    '<bpmndi:BPMNLabel><dc:Bounds x="80" y="140" width="76" height="14"/></bpmndi:BPMNLabel>'
    '</bpmndi:BPMNShape>')


def test_external_labels_are_read_and_drawn():
    """Events carry their name in a separate BPMNLabel with its own coordinates. Ignore that and
    you draw a diagram in which no event is named -- in one real model, 18 of 29 labels."""
    d = bpmn.parse(WITH_EXTERNAL_LABEL)
    s1 = next(s for s in d.shapes if s.element_id == "S1")
    assert s1.label == (80.0, 140.0, 76.0, 14.0)
    svg = bpmn.to_svg(d)
    assert "Start" in svg, "the event name has to appear in the SVG"
    assert 'x="118"' in svg, "and at the position the model specifies"


def test_event_names_appear_even_without_own_label_coordinates():
    d = bpmn.parse(MINI)
    svg = bpmn.to_svg(d)
    for name in ("Start", "End", "or", "does something"):
        assert name in svg, f"{name} is missing from the diagram"


def test_a_long_task_name_is_wrapped():
    long_name = MINI.replace('name="does something"', 'name="checks the coverage and writes the result back"')
    svg = bpmn.to_svg(bpmn.parse(long_name))
    assert svg.count("<tspan") >= 2, "without wrapping half the name sits outside the box"


def test_type_markers_are_drawn():
    svg = bpmn.to_svg(bpmn.parse(MINI))
    assert "bpmn-marker" in svg, "without gear/person glyphs every activity looks the same"


def test_the_event_kind_is_detected():
    with_error = MINI.replace('<bpmn:endEvent id="E1" name="End"/>',
                              '<bpmn:endEvent id="E1" name="End">'
                              '<bpmn:errorEventDefinition id="err1"/></bpmn:endEvent>')
    d = bpmn.parse(with_error)
    e1 = next(s for s in d.shapes if s.element_id == "E1")
    assert e1.event_kind == "error"
    assert e1.throwing is True


def test_multi_instance_is_detected():
    with_mi = MINI.replace('<bpmn:serviceTask id="T1" name="does something"/>',
                          '<bpmn:serviceTask id="T1" name="does something">'
                          '<bpmn:multiInstanceLoopCharacteristics/></bpmn:serviceTask>')
    t1 = next(s for s in bpmn.parse(with_mi).shapes if s.element_id == "T1")
    assert t1.multi_instance is True


def test_label_coordinates_extend_the_drawing_area():
    """Otherwise a label below the last element gets cut off."""
    d = bpmn.parse(WITH_EXTERNAL_LABEL)
    _x0, _y0, _x1, y1 = d.bounds()
    assert y1 >= 154, "the bottom edge of the label has to be inside the area"


@pytest.mark.integration
def test_a_real_model_is_labelled_completely(db, busiest_def_key):
    proc_def_id = db.scalar(
        "SELECT proc_def_id_ FROM act_hi_procinst WHERE proc_def_key_ = %s LIMIT 1",
        (busiest_def_key,))
    d = bpmn.parse(instance_db.load_bpmn_xml(db, proc_def_id))
    named = [s for s in d.shapes if s.name]
    svg = bpmn.to_svg(d, visited={s.element_id for s in d.shapes[:5]})
    missing = [s.name for s in named if s.name.split()[0] not in svg]
    assert not missing, f"labels that were not drawn: {missing[:5]}"
    assert svg.count("bpmn-marker") >= 5
