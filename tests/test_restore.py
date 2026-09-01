"""Tests for ``cib7explorer/restore/docker_restore.py``.

Every test that could create Docker resources runs exclusively against a throwaway profile with
its own container and volume names -- never against a real restored database. The only test that
touches a real one is the ``@pytest.mark.integration`` test below, and it deliberately calls
read-only inspection functions only (``status``, ``ensure_ready`` without ``force``): no
``reset``, no ``--force``, no ``docker rm``/``docker volume rm``.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from cib7explorer import config
from cib7explorer.config import Profile
from cib7explorer.contracts import RestorePhase, RestoreState
from cib7explorer.restore import docker_restore
from cib7explorer.restore.docker_restore import RestoreError

# A real dump does not live in the repository (several GB, real data). To run this test, point
# CIB7_TEST_DUMP at a .backup file; without the variable the test skips itself.
_REAL_DUMP = Path(os.environ.get("CIB7_TEST_DUMP") or "/nonexistent.backup")


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """A CIB7_STATE_DIR/CIB7_CONFIG_DIR of its own for unit tests -- it never touches the real
    environment. The integration test deliberately does NOT request it."""
    monkeypatch.setenv("CIB7_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CIB7_CONFIG_DIR", str(tmp_path / "config"))
    yield tmp_path


# --- dump_fingerprint --------------------------------------------------------------------

def _write_with_fixed_mtime(path: Path, data: bytes, mtime_ns: int) -> None:
    path.write_bytes(data)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_dump_fingerprint_same_content_and_mtime_match(tmp_path):
    data = (b"x" * (2 * 1024 * 1024)) + b"tail-marker"
    mtime = 1_700_000_000_000_000_000
    p1 = tmp_path / "a.backup"
    p2 = tmp_path / "b.backup"
    _write_with_fixed_mtime(p1, data, mtime)
    _write_with_fixed_mtime(p2, data, mtime)

    assert docker_restore.dump_fingerprint(p1) == docker_restore.dump_fingerprint(p2)


def test_dump_fingerprint_changes_with_leading_byte(tmp_path):
    base = bytearray(b"y" * (2 * 1024 * 1024))
    mtime = 1_700_000_000_000_000_000
    p1 = tmp_path / "base.backup"
    _write_with_fixed_mtime(p1, bytes(base), mtime)
    fp_base = docker_restore.dump_fingerprint(p1)

    changed = bytearray(base)
    changed[0] ^= 0xFF
    p2 = tmp_path / "head_changed.backup"
    _write_with_fixed_mtime(p2, bytes(changed), mtime)  # same mtime -- only the content differs

    assert docker_restore.dump_fingerprint(p2) != fp_base


def test_dump_fingerprint_changes_with_trailing_byte(tmp_path):
    base = bytearray(b"z" * (2 * 1024 * 1024))
    mtime = 1_700_000_000_000_000_000
    p1 = tmp_path / "base.backup"
    _write_with_fixed_mtime(p1, bytes(base), mtime)
    fp_base = docker_restore.dump_fingerprint(p1)

    changed = bytearray(base)
    changed[-1] ^= 0xFF
    p2 = tmp_path / "tail_changed.backup"
    _write_with_fixed_mtime(p2, bytes(changed), mtime)

    assert docker_restore.dump_fingerprint(p2) != fp_base


@pytest.mark.skipif(not _REAL_DUMP.exists(), reason="no real dump file available")
def test_dump_fingerprint_is_fast_on_real_dump():
    started = time.perf_counter()
    fp = docker_restore.dump_fingerprint(_REAL_DUMP)
    elapsed = time.perf_counter() - started
    assert isinstance(fp, str) and len(fp) == 64
    assert elapsed < 2.0, f"dump_fingerprint took {elapsed:.2f}s (limit: 2s)"


# --- The progress-line parser (update_state_with_line) ------------------------------------

def _base_state() -> RestoreState:
    return RestoreState(
        profile_name="test",
        dump_path="/x.backup",
        dump_size_bytes=123,
        dump_fingerprint="fp",
        toc_items_total=434,
    )


def test_progress_lines_from_spec_examples():
    state = _base_state()
    lines = [
        "pg_restore: finished item 3629 INDEX act_idx_hi_pi_pdefid_end_time",
        'pg_restore: processing data for table "public.act_hi_procinst"',
        "pg_restore: launching item 12 TABLE DATA act_hi_varinst",
    ]
    for line in lines:
        state = docker_restore.update_state_with_line(state, line)

    # "finished item" counts exactly once, regardless of the object type.
    assert state.toc_items_done == 1
    # Neither "processing" nor "launching" marks a table as finished.
    assert state.tables_done == []
    # current_item reflects the most recent activity seen -- here the last line.
    assert state.current_item == "TABLE DATA act_hi_varinst"
    assert state.log_tail == lines


def test_progress_line_finished_table_data_marks_table_done():
    state = _base_state()
    state = docker_restore.update_state_with_line(
        state, 'pg_restore: processing data for table "public.act_hi_procinst"'
    )
    state = docker_restore.update_state_with_line(
        state, "pg_restore: finished item 20 TABLE DATA act_hi_procinst"
    )
    assert state.toc_items_done == 1
    assert state.tables_done == ["act_hi_procinst"]
    assert state.current_item == "TABLE DATA act_hi_procinst"


def test_progress_log_tail_capped_at_40():
    state = _base_state()
    for i in range(50):
        state = docker_restore.update_state_with_line(state, f"pg_restore: finished item {i} INDEX x{i}")
    assert len(state.log_tail) == 40
    assert state.toc_items_done == 50
    assert state.log_tail[-1] == "pg_restore: finished item 49 INDEX x49"


def test_progress_line_unrecognized_is_kept_in_log_but_ignored():
    state = _base_state()
    state = docker_restore.update_state_with_line(state, "pg_restore: some other diagnostic line")
    assert state.toc_items_done == 0
    assert state.tables_done == []
    assert state.log_tail == ["pg_restore: some other diagnostic line"]


# --- probe_dump-Header-Parser (parse_pg_restore_list_header) --------------------------------

_REAL_HEADER = """;
; Archive created at 2026-08-18 13:29:00 UTC
;     dbname: camunda
;     TOC Entries: 434
;     Compression: -1
;     Dump Version: 1.14-0
;     Format: CUSTOM
;     Integrity: off
;     Dumped from database version: 17.11 (Debian 17.11-1.pgdg13+2)
;     Dumped by pg_dump version: 17.11 (Debian 17.11-1.pgdg13+2)
;
;
1234; 1259 12345 TABLE public act_hi_procinst camunda
"""


def test_parse_pg_restore_list_header_extracts_fields():
    info = docker_restore.parse_pg_restore_list_header(_REAL_HEADER)
    assert info["format"] == "CUSTOM"
    assert info["database_name"] == "camunda"
    assert info["toc_items_total"] == 434
    assert info["source_server_version"] == "17.11"
    assert info["source_server_version_major"] == 17
    assert "17.11 (Debian 17.11-1.pgdg13+2)" == info["source_server_version_raw"]


def test_parse_pg_restore_list_header_rejects_non_custom_format():
    text = _REAL_HEADER.replace("Format: CUSTOM", "Format: DIRECTORY")
    with pytest.raises(RestoreError):
        docker_restore.parse_pg_restore_list_header(text)


def test_pick_restore_image_profile_override_wins():
    profile = Profile(name="x", image="registry.local/postgres:custom")
    probe = {"recommended_image": "postgres:17-alpine"}
    assert docker_restore._pick_restore_image(profile, probe) == "registry.local/postgres:custom"


def test_pick_restore_image_uses_probe_recommendation():
    profile = Profile(name="x")
    probe = {"recommended_image": "postgres:16-alpine"}
    assert docker_restore._pick_restore_image(profile, probe) == "postgres:16-alpine"


def test_pick_restore_image_falls_back_without_recommendation():
    profile = Profile(name="x")
    assert docker_restore._pick_restore_image(profile, {}) == docker_restore._PROBE_IMAGE_FALLBACK


# --- redact --------------------------------------------------------------------

def test_ensure_ready_redacts_password_in_failure(isolated_state, monkeypatch):
    secret = "s3cr3t-Passw0rt-xyz"

    def fake_docker_available():
        return False, f"docker reports an internal error, password={secret} while connecting"

    monkeypatch.setattr(docker_restore, "docker_available", fake_docker_available)

    profile = Profile(name="redact-test", dump_file=str(_REAL_DUMP) if _REAL_DUMP.exists() else "/x.backup")
    final = docker_restore.ensure_ready(profile)

    assert final.phase is RestorePhase.FAILED
    assert secret not in final.error
    assert "***" in final.error


def test_config_redact_used_directly_removes_uri_password():
    text = "connection failed: postgresql://user:SecretPassword1@host:5432/db"
    cleaned = config.redact(text)
    assert "SecretPassword1" not in cleaned


# --- start_background: a duplicate call -------------------------------------------------

def test_start_background_rejects_second_call_while_running(isolated_state, monkeypatch):
    profile = Profile(name="bg-test", dump_file="/does/not/exist.backup")
    started = threading.Event()
    release = threading.Event()

    def fake_ensure_ready(p, *, progress=None, force=False):
        started.set()
        release.wait(timeout=5)
        return RestoreState(profile_name=p.name, dump_path="", dump_size_bytes=0, dump_fingerprint="")

    monkeypatch.setattr(docker_restore, "ensure_ready", fake_ensure_ready)

    docker_restore.start_background(profile)
    assert started.wait(timeout=2), "the background restore did not start in time"

    with pytest.raises(RestoreError):
        docker_restore.start_background(profile)

    release.set()
    time.sleep(0.2)  # give the thread a chance to remove itself from the registry


# --- integration test: adopt an existing restored database ---------------------------------

@pytest.mark.integration
def test_status_and_ensure_ready_adopt_an_existing_dump():
    """An existing container with its volume should be adopted rather than reloaded. This test
    deliberately calls NEITHER `force` NOR `reset`."""
    name = os.environ.get("CIB7_TEST_PROFILE", "demo-dump")
    try:
        profile = config.get_profile(name)
    except KeyError:
        pytest.skip(f"profile '{name}' is not defined -- integration test skipped.")

    avail, msg = docker_restore.docker_available()
    if not avail:
        pytest.skip(f"docker not available: {msg}")

    state = docker_restore.status(profile)
    if state.phase is not RestorePhase.READY:
        pytest.skip(
            f"The expected database is not present (phase={state.phase.value}, "
            f"message={state.message!r}) -- not a defect of this test but a missing local "
            "local environment."
        )

    assert state.phase is RestorePhase.READY
    assert state.adopted_existing is True

    started = time.perf_counter()
    final = docker_restore.ensure_ready(profile)  # NO force=True!
    elapsed = time.perf_counter() - started

    assert final.phase is RestorePhase.READY
    assert final.adopted_existing is True
    # A real restore of a multi-gigabyte dump would take minutes -- returning quickly is the
    # evidence that ensure_ready did NOT reload anything.
    assert elapsed < 20.0, (
        f"ensure_ready took {elapsed:.1f}s -- that looks like a real restore rather than an "
        "adoption."
    )


# --- regression test for a failure that actually happened ----------------------------------

def test_stop_container_gracefully_sends_stop_before_rm(monkeypatch):
    """The restore container runs with fsync=off. Killing it with `docker rm -f` (SIGKILL) loses
    the last transactions -- which once happened to be exactly the read-only role's GRANTs, so the
    role could no longer read a single table while the restore reported "finished". Hence a
    `docker stop` has to come before the removal.
    """
    from cib7explorer.restore import docker_restore as rst

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(rst.subprocess, "run", fake_run)
    rst._stop_container_gracefully("cib7-anything")

    verbs = [c[1] for c in calls if len(c) > 1]
    assert verbs == ["stop", "rm"], f"expected stop before rm, was {verbs}"
    assert "-t" in calls[0], "docker stop needs a grace period for the shutdown"


def test_verify_read_only_access_raises_when_role_cannot_read(monkeypatch):
    """A restore after which the read-only role cannot read must not count as finished."""
    from cib7explorer import config
    from cib7explorer.restore import docker_restore as rst

    prof = config.Profile(name="x", container="c", database="camunda", user="explorer_ro")

    class R:
        returncode = 1
        stdout = ""
        stderr = "psql: error: permission denied for table act_hi_procinst (pw=secret123456)"

    monkeypatch.setattr(rst.subprocess, "run", lambda *a, **kw: R())
    try:
        rst._verify_read_only_access(prof, "explorer_ro", "secret123456")
    except rst.RestoreError as exc:
        assert "cannot read act_hi_procinst" in str(exc)
        assert "secret123456" not in str(exc), "the password must not appear in the error message"
    else:
        raise AssertionError("expected a RestoreError")


def test_the_docker_check_is_cached(monkeypatch):
    """`docker info` costs close to a second with a running daemon and hangs until its timeout
    with a stopped one. The start page calls it on every request -- without a cache that once
    turned a two-second unit suite into a 161-second one."""
    from cib7explorer.restore import docker_restore as rst

    calls = {"n": 0}

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    def counting(*a, **kw):
        calls["n"] += 1
        return R()

    monkeypatch.setattr(rst.subprocess, "run", counting)
    monkeypatch.setattr(rst, "_docker_cache", None)
    assert rst.docker_available()[0] is True
    assert rst.docker_available()[0] is True
    assert rst.docker_available()[0] is True
    assert calls["n"] == 1, f"docker info was called {calls['n']} times"

    assert rst.docker_available(recheck=True)[0] is True
    assert calls["n"] == 2, "an explicit recheck has to get through"


def test_the_docker_timeout_is_short():
    from cib7explorer.restore import docker_restore as rst

    assert rst._DOCKER_TIMEOUT <= 5, "a stopped daemon must not slow the interface down"
