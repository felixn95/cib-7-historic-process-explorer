"""Tests for the variable catalogue.

The most important test in this file is ``test_no_sql_reads_a_variable_value``: the catalogue must
not read variable values, not even when the value mode is on, and its output has to be shareable
without values in it. That is a guarantee about the code, not merely an intention in a docstring.
"""

from __future__ import annotations

import re

import pytest

from cib7explorer.contracts import (
    CatalogMeta,
    CrossProcessVariable,
    SerializationForm,
    SizeStats,
    VariableCatalog,
    VariableCatalogEntry,
)
from cib7explorer.db import sqlguard, varcatalog


def _entry(**kw) -> VariableCatalogEntry:
    base = dict(def_key="order-8000", name="orderNumber", types=("string",),
                occurrences=100, instances_with=90, def_instances=100)
    base.update(kw)
    return VariableCatalogEntry(**base)


# --- the iron rule: no values ---------------------------------------------------------------

_VALUE_COLUMNS = ("text_", "text2_", "bytes_", "long_", "double_")


def _sql_constants() -> dict[str, str]:
    return {n: v for n, v in vars(varcatalog).items()
            if n.startswith("_SQL_") and isinstance(v, str)}


def test_there_are_sql_constants_at_all():
    assert len(_sql_constants()) >= 5, "SQL should live in _SQL_* constants, so it can be checked"


def test_every_sql_constant_passes_the_guard():
    for name, sql in _sql_constants().items():
        sqlguard.check(sql)


def test_no_sql_reads_a_variable_value():
    """`text_`/`bytes_` and friends may appear only inside length()/octet_length(), and `text2_`
    only in the serialisation query -- where it is the Java class name, i.e. a type name and not a
    value."""
    for name, sql in _sql_constants().items():
        unpacked = re.sub(r"(?:octet_)?length\s*\(\s*[a-z_.]*(?:text_|bytes_)\s*\)", " LENGTH ", sql,
                          flags=re.IGNORECASE)
        for column in _VALUE_COLUMNS:
            if column == "text2_" and "SERIAL" in name.upper():
                continue        # an explicit, documented exception
            hits = re.search(rf"\b{column}", unpacked)
            assert not hits, (
                f"{name} touches {column} without wrapping it in a length measurement: "
                f"{unpacked[max(0, hits.start() - 60):hits.end() + 20]!r}"
            )


def test_the_csv_has_no_value_column():
    cat = VariableCatalog(entries=(_entry(),), cross_process=(), meta=CatalogMeta(rows=1))
    csv_text = varcatalog.to_csv(cat)
    columns = [c.strip().lower() for c in csv_text.lstrip("\ufeff").splitlines()[0].split(";")]

    # Raw value columns of the database must not appear at all.
    for raw in _VALUE_COLUMNS:
        assert not any(raw in c for c in columns), f"{raw!r} has no business in the export"

    # And no column may be THE value. "Share with value (%)" or "Null-typed (no value)" are
    # counts and explicitly fine -- they say how often a value is present, not which one.
    forbidden = {"value", "values", "text", "bytes", "content", "variable value"}
    assert not (forbidden & set(columns)), f"value column in the export: {forbidden & set(columns)}"


def test_the_csv_opens_in_spreadsheet_software():
    cat = VariableCatalog(entries=(_entry(),), meta=CatalogMeta(rows=1))
    csv_text = varcatalog.to_csv(cat)
    assert csv_text.startswith("﻿"), "BOM missing; spreadsheet software then misreads UTF-8"
    assert ";" in csv_text.splitlines()[0]
    assert "orderNumber" in csv_text


# --- shares and denominators ----------------------------------------------------------------

def test_a_share_without_a_denominator_is_none_not_a_crash():
    e = _entry(def_instances=0, instances_with=0)
    assert e.share_of_instances is None
    assert e.share_with_value is None


def test_a_share_with_its_denominator():
    e = _entry(def_instances=8000, instances_with=7900)
    assert e.share_of_instances == pytest.approx(7900 / 8000)


def test_with_a_value_differs_from_present():
    """Hundreds of thousands of variable instances can carry the type 'null': they exist but
    hold no value. The two must not be the same number."""
    e = _entry(occurrences=100, null_typed=40, instances_with=100, def_instances=100)
    assert e.share_of_instances == 1.0
    assert e.share_with_value == pytest.approx(0.6)


# --- resolvability --------------------------------------------------------------------------

@pytest.mark.parametrize("var_type,java_class,expected", [
    ("json", None, True),
    ("string", None, True),
    ("serializable", "java.time.LocalDateTime", True),
    ("serializable", "java.util.ArrayList", True),
    ("serializable", "com.example.app.OwnClass", False),
    ("serializable", None, None),
])
def test_resolvability_without_the_application(var_type, java_class, expected):
    assert SerializationForm(var_type, java_class, 1).resolvable_without_application is expected


def test_an_entry_is_unresolvable_if_any_form_is():
    e = _entry(serialization=(SerializationForm("json", None, 5),
                              SerializationForm("serializable", "com.example.X", 2)))
    assert e.resolvability is False


# --- cross-process view ---------------------------------------------------------------------

def test_cross_process_only_names_in_more_than_one_definition():
    entries = [
        _entry(def_key="a", name="orderNumber", types=("string",)),
        _entry(def_key="b", name="orderNumber", types=("string",)),
        _entry(def_key="c", name="orderNumber", types=("long",)),
        _entry(def_key="a", name="onlyHere", types=("string",)),
    ]
    cross = varcatalog.build_cross_process(entries)
    names = {c.name: c for c in cross}
    assert "onlyHere" not in names, "a name in only one definition is not a candidate"
    c = names["orderNumber"]
    assert c.def_count == 3
    assert set(c.definitions) == {"a", "b", "c"}
    assert c.type_conflict is True
    assert set(c.types) == {"string", "long"}


def test_cross_process_without_a_type_conflict():
    entries = [_entry(def_key="a", name="x", types=("string",)),
               _entry(def_key="b", name="x", types=("string",))]
    (c,) = varcatalog.build_cross_process(entries)
    assert c.type_conflict is False


def test_cross_process_is_a_candidate_not_a_claim():
    """The docstring has to carry the caveat -- it is the substance of the claim."""
    text = (varcatalog.build_cross_process.__doc__ or "") + (CrossProcessVariable.__doc__ or "")
    assert "candidate" in text


# --- Integration ------------------------------------------------------------------------

@pytest.mark.integration
def test_catalogue_against_a_real_database(catalog):
    cat = catalog

    assert len(cat.entries) > 1000, "this database yields thousands of catalogue rows"
    assert cat.meta.rows == len(cat.entries)
    assert cat.meta.built_at is not None
    assert cat.meta.notes, "the caveats have to travel with the result"

    with_type_change = [e for e in cat.entries if e.type_switch]
    assert with_type_change, "type switches do occur in this database"
    assert all(len(e.types) > 1 for e in with_type_change)

    assert any(e.from_call_activity for e in cat.entries), "values from call activities do occur"
    largest = max((e.bytearray_size.maximum or 0) for e in cat.entries)
    assert largest > 1_000_000, f"the largest bytearray value was {largest}"

    # Denominator: never a share above 1
    for e in cat.entries:
        share = e.share_of_instances
        assert share is None or share <= 1.0000001, f"{e.def_key}/{e.name}: share {share}"

    cross = cat.cross_process or varcatalog.build_cross_process(cat.entries)
    # Every cross-process entry has to earn the name: present in more than one definition.
    for c in cross:
        assert len(c.definitions) > 1, f"{c.name} appears in {len(c.definitions)} definition(s)"
        assert c.def_count == len(c.definitions)
    print(f"\ncatalogue: {len(cat.entries)} entries, {len(cross)} cross-process, "
          f"built in {cat.meta.duration_ms} ms")
