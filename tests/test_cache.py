"""Tests for the local precomputation cache (cib7explorer/cache.py).

Every test runs against a temporary CIB7_STATE_DIR so that none of them touches the real cache.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cib7explorer.cache import Cache
from cib7explorer.contracts import Classification, DetectionResult, SchemaDeviation, TableInfo


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CIB7_STATE_DIR", str(tmp_path / "state"))
    yield


def _make_cache(**overrides) -> Cache:
    kwargs = dict(installation_id="inst-123", schema_fingerprint="fp-aaa", profile_name="demo-dump")
    kwargs.update(overrides)
    return Cache(**kwargs)


def _make_detection(*, table_exists: bool = True, deviation: bool = False) -> DetectionResult:
    tables = [
        TableInfo(name="act_hi_procinst", exists=table_exists, est_rows=200000, total_bytes=1000, has_rows=True),
        TableInfo(name="act_re_procdef", exists=True, est_rows=201, total_bytes=100, has_rows=True),
    ]
    deviations = []
    if deviation:
        deviations.append(SchemaDeviation(table="act_hi_procinst", kind="missing_column", detail="foo is missing"))
    return DetectionResult(
        profile_name="demo-dump",
        classification=Classification.TEST,
        server_version="17.11",
        database_name="camunda",
        connected_as="explorer_ro",
        session_is_read_only=True,
        installation_id="inst-123",
        engine_schema_version="7.23.0",
        tables=tables,
        deviations=deviations,
    )


# --- put / get / the JSON round trip -------------------------------------------------------------

def test_put_get_roundtrip_with_datetime():
    cache = _make_cache()
    now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
    payload = {"count": 42, "when": now, "names": ["a", "b"]}

    cache.put("mykey", payload, source_note="test run")
    result = cache.get("mykey")

    assert result is not None
    got_payload, created_at = result
    # A datetime inside the payload comes back as a string (JSON has no datetime) -- that is the
    # expected behaviour of `default=str`, not a round-trip bug.
    assert got_payload["count"] == 42
    assert got_payload["names"] == ["a", "b"]
    assert isinstance(got_payload["when"], str)
    assert got_payload["when"] == str(now)
    # created_at (metadata of the entry, not part of the payload) is a real datetime.
    assert isinstance(created_at, datetime)
    assert created_at.tzinfo is not None


def test_get_missing_key_returns_none():
    cache = _make_cache()
    assert cache.get("does-not-exist") is None


def test_put_overwrites_existing_key():
    cache = _make_cache()
    cache.put("k", {"v": 1})
    cache.put("k", {"v": 2})
    payload, _ = cache.get("k")
    assert payload == {"v": 2}
    assert len(cache.entries()) == 1


# --- age() ----------------------------------------------------------------------------------

def test_age_is_small_right_after_put():
    cache = _make_cache()
    cache.put("k", {"v": 1})
    age = cache.age("k")
    assert age is not None
    assert timedelta(0) <= age < timedelta(seconds=10)


def test_age_missing_key_is_none():
    cache = _make_cache()
    assert cache.age("nope") is None


# --- entries() --------------------------------------------------------------------------------

def test_entries_lists_all_with_metadata():
    cache = _make_cache()
    cache.put("a", {"x": 1}, source_note="first")
    cache.put("b", {"x": 2, "y": "z" * 50}, source_note="second")

    entries = cache.entries()
    keys = {e["key"] for e in entries}
    assert keys == {"a", "b"}
    for e in entries:
        assert set(e) == {"key", "created_at", "bytes", "source_note"}
        assert e["bytes"] > 0
    notes = {e["key"]: e["source_note"] for e in entries}
    assert notes == {"a": "first", "b": "second"}


# --- drop() ------------------------------------------------------------------------------------

def test_drop_single_key():
    cache = _make_cache()
    cache.put("a", {"x": 1})
    cache.put("b", {"x": 2})

    removed = cache.drop("a")
    assert removed == 1
    assert cache.get("a") is None
    assert cache.get("b") is not None


def test_drop_all():
    cache = _make_cache()
    cache.put("a", {"x": 1})
    cache.put("b", {"x": 2})

    removed = cache.drop()
    assert removed == 2
    assert cache.entries() == []


# --- The file name: installation_id plus the schema fingerprint --------------------------------------

def test_filename_changes_with_schema_fingerprint():
    c1 = _make_cache(schema_fingerprint="fp-aaa")
    c2 = _make_cache(schema_fingerprint="fp-bbb")
    assert c1.path != c2.path


def test_filename_changes_with_installation_id():
    c1 = _make_cache(installation_id="inst-A")
    c2 = _make_cache(installation_id="inst-B")
    assert c1.path != c2.path


def test_two_instances_same_fingerprint_share_file():
    c1 = _make_cache(profile_name="profile-one")
    c2 = _make_cache(profile_name="profile-two")  # same installation_id and fingerprint

    assert c1.path == c2.path

    c1.put("shared", {"v": 1})
    payload, _ = c2.get("shared")
    assert payload == {"v": 1}


def test_falls_back_to_profile_name_without_installation_id():
    c1 = _make_cache(installation_id=None, profile_name="demo-dump")
    c2 = _make_cache(installation_id=None, profile_name="other-profile")
    # without an installation_id the profile name decides the file name -> different files
    assert c1.path != c2.path
    assert "demo-dump" in c1.path.name


# --- for_detection() / compute_schema_fingerprint -------------------------------------------

def test_for_detection_uses_installation_id_from_result():
    det = _make_detection()
    cache = Cache.for_detection("demo-dump", det)
    assert cache.installation_id == "inst-123"
    assert cache.profile_name == "demo-dump"


def test_schema_fingerprint_stable_for_equal_detection():
    det_a = _make_detection()
    det_b = _make_detection()
    assert Cache.compute_schema_fingerprint(det_a) == Cache.compute_schema_fingerprint(det_b)


def test_schema_fingerprint_changes_with_deviation():
    det_clean = _make_detection(deviation=False)
    det_deviant = _make_detection(deviation=True)
    assert Cache.compute_schema_fingerprint(det_clean) != Cache.compute_schema_fingerprint(det_deviant)


def test_schema_fingerprint_changes_with_missing_table():
    det_present = _make_detection(table_exists=True)
    det_missing = _make_detection(table_exists=False)
    assert Cache.compute_schema_fingerprint(det_present) != Cache.compute_schema_fingerprint(det_missing)
