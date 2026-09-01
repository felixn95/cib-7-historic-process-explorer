"""Restore a dump into a local, containerised PostgreSQL instance.

The idea: getting from "I have a dump file" to "I can explore it" should be one command, with
visible progress and without blocking the interface. A multi-gigabyte restore runs in the
background, its state is written to disk after every step (``restores_dir()/<profile>.json``),
and the interface polls that file instead of waiting on a callback.

State machine (see ``RestorePhase`` in contracts.py); every step is persisted:

    CHECKING -> CREATING_CONTAINER -> RESTORING -> POST_PROCESSING -> READY
                                                                   \\-> FAILED (at any point)

Repeatability after an abort: a run interrupted during RESTORING leaves a half-filled volume
behind. That is deliberately NOT treated as resumable -- ``pg_restore`` offers no clean
restart point for a custom-format dump processed by several parallel jobs. The only way
forward is ``reset()`` (discard container and volume) or ``ensure_ready(..., force=True)``,
followed by a full restore. The error message says so, instead of inventing an incremental
resume that would silently produce an incomplete database.

Every docker call goes through ``subprocess`` with list arguments, never ``shell=True``.
Passwords are never embedded in visible command-line arguments or in SQL text: the admin
password is passed as ``-e PGPASSWORD=...`` to ``docker exec``/``docker run``, and the
read-only role's password is read into psql via ``\\getenv`` from an environment variable, so
it does not appear in the SQL text and therefore cannot end up in a database log.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import Profile, redact, restores_dir, ensure_dirs
from ..contracts import RestorePhase, RestoreState

log = logging.getLogger("cib7explorer.restore")

#: Core tables whose presence AND content count as evidence of a restored database.
_CORE_TABLES = ("act_hi_procinst", "act_re_procdef")

#: Image used purely to read the dump header (``pg_restore -l``). Deliberately independent of
#: the dump's source version: ``pg_restore -l`` lists only the table of contents and reads no
#: data, which is stable across PostgreSQL major versions. First attempt with a current, known
#: version; if that call fails (because the image cannot be pulled, say), a second attempt with
#: the Alpine "latest" variant.
_PROBE_IMAGE_PRIMARY = "postgres:17-alpine"
_PROBE_IMAGE_FALLBACK = "postgres:alpine"

_ROLE_CAMUNDA = "camunda"


class RestoreError(RuntimeError):
    """A restore failure -- the message has already been redacted."""


# --- state file ----------------------------------------------------------------------------

def _state_path_for(name: str) -> Path:
    return restores_dir() / f"{name}.json"


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def _save_state(state: RestoreState) -> None:
    ensure_dirs()
    path = _state_path_for(state.profile_name)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(dataclasses.asdict(state), ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    tmp.replace(path)


def _state_from_dict(d: dict) -> RestoreState:
    data = dict(d)
    phase = data.get("phase")
    try:
        data["phase"] = RestorePhase(phase) if phase else RestorePhase.ABSENT
    except ValueError:
        data["phase"] = RestorePhase.ABSENT
    for key in ("started_at", "finished_at"):
        v = data.get(key)
        if isinstance(v, str) and v:
            try:
                data[key] = datetime.fromisoformat(v)
            except ValueError:
                data[key] = None
        else:
            data[key] = None
    known = {f.name for f in dataclasses.fields(RestoreState)}
    data = {k: v for k, v in data.items() if k in known}
    return RestoreState(**data)


def read_state(profile: Profile) -> RestoreState:
    """Read a profile's restore state; without a state file, a fresh ABSENT state.

    Even without a state file, the dump's size and fingerprint are determined right away --
    that is what lets ``ensure_ready`` later recognise whether an existing database belongs to
    the dump file currently configured.
    """
    path = _state_path_for(profile.name)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return _state_from_dict(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("state file %s is corrupt (%s) -- rebuilding the initial state.", path, exc)

    dump_path = Path(profile.dump_file) if profile.dump_file else None
    size = 0
    fp = ""
    if dump_path is not None and dump_path.is_file():
        try:
            size = dump_path.stat().st_size
            fp = dump_fingerprint(dump_path)
        except OSError as exc:
            log.warning("dump file %s is not readable: %s", dump_path, exc)
    return RestoreState(
        profile_name=profile.name,
        dump_path=str(dump_path) if dump_path is not None else (profile.dump_file or ""),
        dump_size_bytes=size,
        dump_fingerprint=fp,
        phase=RestorePhase.ABSENT,
    )


# --- fingerprint of the dump file ----------------------------------------------------------

def dump_fingerprint(path: Path) -> str:
    """Cheap, stable fingerprint of a dump file that may be several gigabytes.

    Deliberately does NOT read the whole file -- that would take minutes for no gain. Instead:
    file size + mtime_ns + sha256 of the first 1 MiB + sha256 of the last 1 MiB.
    """
    st = path.stat()
    chunk = 1024 * 1024
    with path.open("rb") as f:
        head = f.read(chunk)
        if st.st_size > chunk:
            f.seek(max(0, st.st_size - chunk))
            tail = f.read(chunk)
        else:
            tail = head
    head_hash = hashlib.sha256(head).hexdigest()
    tail_hash = hashlib.sha256(tail).hexdigest()
    combined = f"{st.st_size}:{st.st_mtime_ns}:{head_hash}:{tail_hash}"
    return hashlib.sha256(combined.encode("ascii")).hexdigest()


# --- reading the dump header (pg_restore -l) -----------------------------------------------

_RE_HEADER_KV = re.compile(r"^;\s*([^:]+):\s*(.*)$")
_RE_VERSION = re.compile(r"^\s*(\d+)\.(\d+)")


def parse_pg_restore_list_header(text: str) -> dict:
    """Parse the comment lines of ``pg_restore -l`` -- pure text processing, no Docker.

    Expected lines include:
        ;     Format: CUSTOM
        ;     dbname: camunda
        ;     TOC Entries: 434
        ;     Dumped from database version: 17.11 (Debian 17.11-1.pgdg13+2)
    """
    info: dict[str, Any] = {}
    for raw_line in text.splitlines():
        m = _RE_HEADER_KV.match(raw_line)
        if not m:
            continue
        key = m.group(1).strip()
        val = m.group(2).strip()
        if key == "Format":
            info["format"] = val
        elif key == "dbname":
            info["database_name"] = val
        elif key == "TOC Entries":
            try:
                info["toc_items_total"] = int(val)
            except ValueError:
                pass
        elif key == "Dumped from database version":
            info["source_server_version_raw"] = val
            vm = _RE_VERSION.match(val)
            if vm:
                info["source_server_version"] = f"{vm.group(1)}.{vm.group(2)}"
                info["source_server_version_major"] = int(vm.group(1))
    if info.get("format") != "CUSTOM":
        raise RestoreError(
            f"The dump is not in a supported format (found: {info.get('format', 'unknown')}); "
            "CUSTOM is expected (pg_dump -Fc)."
        )
    return info


def _run_pg_restore_list(path: Path, image: str) -> str:
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{path}:/dump/db.backup:ro",
        image,
        "pg_restore", "-l", "/dump/db.backup",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        raise RestoreError("docker is not installed or not on PATH.") from None
    except subprocess.TimeoutExpired:
        raise RestoreError("Timed out while reading the dump header.") from None
    if proc.returncode != 0:
        raise RestoreError(redact(f"pg_restore -l failed: {proc.stderr.strip()[:500]}"))
    return proc.stdout


def probe_dump(path: Path) -> dict:
    """Read format, source version, database name and TOC count from the dump header.

    Runs ``pg_restore -l`` in a throwaway container (see ``_run_pg_restore_list``) and parses
    the output with ``parse_pg_restore_list_header`` -- kept separate on purpose, so the parsing
    is testable without Docker.
    """
    if not path.is_file():
        raise RestoreError(f"Dump file not found: {path}")
    last_exc: RestoreError | None = None
    for image in (_PROBE_IMAGE_PRIMARY, _PROBE_IMAGE_FALLBACK):
        try:
            text = _run_pg_restore_list(path, image)
            info = parse_pg_restore_list_header(text)
        except RestoreError as exc:
            last_exc = exc
            continue
        major = info.get("source_server_version_major")
        info["recommended_image"] = f"postgres:{major}-alpine" if major else _PROBE_IMAGE_FALLBACK
        return info
    assert last_exc is not None
    raise last_exc


def _pick_restore_image(profile: Profile, probe: dict) -> str:
    """Image for the actual restore container: ``profile.image`` overrides everything."""
    if profile.image:
        return profile.image
    return probe.get("recommended_image") or _PROBE_IMAGE_FALLBACK


# --- pg_restore progress lines -------------------------------------------------------------

_RE_ITEM = re.compile(r"^pg_restore: (finished|launching) item (\d+) (.+)$")
_RE_PROCESSING_TABLE = re.compile(r'^pg_restore: processing data for table "([^"]+)"$')

_LOG_TAIL_MAX = 40


def update_state_with_line(state: RestoreState, line: str) -> RestoreState:
    """A pure state update from a single line of ``pg_restore`` output.

    Separated from process execution so it is testable without Docker. ``toc_items_done`` counts
    completed TOC entries ("finished item ..."), ``tables_done`` collects tables whose data is
    fully loaded ("finished item ... TABLE DATA ..."), and ``current_item`` is the most recent
    activity seen -- including "launching"/"processing" lines, which do not themselves represent
    a completed entry.
    """
    tail = state.log_tail + [line]
    if len(tail) > _LOG_TAIL_MAX:
        tail = tail[-_LOG_TAIL_MAX:]
    toc_items_done = state.toc_items_done
    tables_done = list(state.tables_done)
    current_item = state.current_item

    m = _RE_ITEM.match(line)
    if m:
        verb, _item_no, rest = m.group(1), m.group(2), m.group(3)
        type_, _, name = rest.rpartition(" ")
        current_item = f"{type_} {name}".strip()
        if verb == "finished":
            toc_items_done += 1
            if type_ == "TABLE DATA" and name not in tables_done:
                tables_done.append(name)
    else:
        m2 = _RE_PROCESSING_TABLE.match(line)
        if m2:
            current_item = f"Lade Daten: {m2.group(1)}"

    return replace(
        state,
        log_tail=tail,
        toc_items_done=toc_items_done,
        tables_done=tables_done,
        current_item=current_item,
    )


# --- docker helpers --------------------------------------------------------------------------

#: Cached result of the Docker check, with a timestamp. ``docker info`` costs close to a second
#: with a running daemon and hangs until its timeout when the daemon is off. The start page calls
#: it on every request, and without this cache that alone once turned a two-second test suite
#: into a two-and-a-half-minute one, with nothing wrong in the code.
_docker_cache: tuple[float, tuple[bool, str]] | None = None
_DOCKER_CACHE_SECONDS = 30.0
_DOCKER_TIMEOUT = 4


def docker_available(*, recheck: bool = False) -> tuple[bool, str]:
    """Check ``docker info``; never raises.

    The result is cached briefly: the answer rarely changes, the call is expensive, and a
    stopped daemon must not slow the interface down.
    """
    global _docker_cache
    if not recheck and _docker_cache is not None:
        alter = time.monotonic() - _docker_cache[0]
        if alter < _DOCKER_CACHE_SECONDS:
            return _docker_cache[1]
    result = _docker_available_uncached()
    _docker_cache = (time.monotonic(), result)
    return result


def _docker_available_uncached() -> tuple[bool, str]:
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, text=True,
                              timeout=_DOCKER_TIMEOUT)
    except FileNotFoundError:
        return False, "docker is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return False, (f"docker info did not answer within {_DOCKER_TIMEOUT} s -- is the Docker "
                       "daemon running?")
    except Exception as exc:  # noqa: BLE001 -- catching everything is the point, see docstring
        return False, redact(f"docker info failed: {exc}")
    if proc.returncode != 0:
        return False, redact(f"docker is not reachable: {proc.stderr.strip()[:300]}")
    return True, "docker is available"


def _container_running(name: str) -> bool:
    cmd = ["docker", "inspect", "-f", "{{.State.Running}}", name]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _container_exists(name: str) -> bool:
    cmd = ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return proc.returncode == 0 and name in proc.stdout.split()


def _pg_isready(profile: Profile) -> bool:
    cmd = ["docker", "exec", profile.container_name, "pg_isready",
           "-U", profile.admin_user, "-d", profile.database]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return proc.returncode == 0


def _wait_pg_isready(profile: Profile, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if _pg_isready(profile):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)


def _docker_exec_psql(profile: Profile, admin_pw: str | None, sql: str, *, timeout: int = 30) -> str:
    """Run a single statement via ``-c`` -- only for statements with no secret in the SQL text
    itself (see ``_docker_exec_psql_stdin`` for statements involving a password)."""
    cmd = ["docker", "exec"]
    if admin_pw:
        cmd += ["-e", f"PGPASSWORD={admin_pw}"]
    cmd += [profile.container_name, "psql", "-v", "ON_ERROR_STOP=1",
            "-U", profile.admin_user, "-d", profile.database, "-tAc", sql]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RestoreError(redact(f"psql command failed: {proc.stderr.strip()[:500]}", admin_pw))
    return proc.stdout


def _docker_exec_psql_stdin(
    profile: Profile,
    admin_pw: str | None,
    sql_script: str,
    *,
    extra_env: dict[str, str] | None = None,
    timeout: int = 60,
) -> str:
    """Run an SQL script via stdin. Secrets travel exclusively as environment variables
    (``-e NAME=VALUE``), never as a visible command-line argument and never inside the SQL text
    -- the script reads them with ``\\getenv``."""
    cmd = ["docker", "exec", "-i"]
    if admin_pw:
        cmd += ["-e", f"PGPASSWORD={admin_pw}"]
    for k, v in (extra_env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [profile.container_name, "psql", "-v", "ON_ERROR_STOP=1",
            "-U", profile.admin_user, "-d", profile.database, "-f", "-"]
    secrets = [admin_pw, *(extra_env or {}).values()]
    try:
        proc = subprocess.run(cmd, input=sql_script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RestoreError("psql script exceeded its time limit.") from None
    if proc.returncode != 0:
        raise RestoreError(redact(f"psql script failed: {proc.stderr.strip()[:800]}", *secrets))
    return proc.stdout


def _core_tables_present(profile: Profile, admin_pw: str | None = None) -> bool:
    checks = " and ".join(f"to_regclass('public.{t}') is not null" for t in _CORE_TABLES)
    sql = (
        f"select ({checks}) and exists(select 1 from {_CORE_TABLES[0]} limit 1)"
    )
    try:
        out = _docker_exec_psql(profile, admin_pw, sql)
    except RestoreError:
        return False
    return out.strip() == "t"


# --- container lifecycle ---------------------------------------------------------------------

_RESTORE_TUNING = [
    "-c", "shared_buffers=1536MB", "-c", "maintenance_work_mem=512MB", "-c", "work_mem=64MB",
    "-c", "max_wal_size=8GB", "-c", "min_wal_size=1GB", "-c", "checkpoint_timeout=30min",
    "-c", "fsync=off", "-c", "full_page_writes=off", "-c", "synchronous_commit=off",
    "-c", "autovacuum=off", "-c", "max_connections=50",
]

_NORMAL_TUNING = [
    "-c", "fsync=on", "-c", "autovacuum=on",
    "-c", "shared_buffers=1536MB", "-c", "work_mem=64MB",
]


def _create_container(profile: Profile, image: str, *, force: bool) -> str:
    """Create volume and container with restore tuning; returns the admin password."""
    name = profile.container_name
    if _container_exists(name):
        if not force:
            raise RestoreError(
                f"Container '{name}' already exists, but its data cannot be matched "
                "unambiguously to the current dump. Repeat the restore with --force to discard "
                "container and volume and start over."
            )
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30)

    if force:
        subprocess.run(["docker", "volume", "rm", "-f", profile.volume_name],
                        capture_output=True, text=True, timeout=30)
    subprocess.run(["docker", "volume", "create", profile.volume_name],
                    capture_output=True, text=True, timeout=30)

    admin_pw = profile.admin_password(create_if_managed=True)
    if not admin_pw:
        raise RestoreError("No admin password available (this profile is not a managed container).")

    cmd = [
        "docker", "run", "-d", "--name", name,
        "-v", f"{profile.volume_name}:/var/lib/postgresql/data",
        "-v", f"{profile.dump_file}:/dump/db.backup:ro",
        "-p", f"127.0.0.1:{profile.port}:5432",
        "-e", f"POSTGRES_PASSWORD={admin_pw}",
        "-e", f"POSTGRES_DB={profile.database}",
        "--shm-size=1g",
        image,
        *_RESTORE_TUNING,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RestoreError(redact(f"docker run failed: {proc.stderr.strip()[:500]}", admin_pw))

    if not _wait_pg_isready(profile, timeout_s=120):
        raise RestoreError(
            "PostgreSQL in the new container did not become ready in time (pg_isready)."
        )
    return admin_pw


def _stop_container_gracefully(name: str, *, timeout_s: int = 180) -> None:
    """Shut the container down cleanly, NOT with SIGKILL.

    This is not cosmetic. The restore container deliberately runs with ``fsync=off`` and
    ``synchronous_commit=off`` so that a multi-gigabyte restore finishes in minutes rather than
    hours. Killing a PostgreSQL configured that way (``docker rm -f``) loses the last
    transactions: on the next start the log says "database system was not properly shut down"
    and recovery replays only what actually reached the WAL on disk.

    That is precisely how the read-only role's GRANTs once disappeared while the role attributes
    -- which live in the global catalog -- survived: a role that could log in but could not see a
    single table. ``docker stop`` sends SIGTERM, PostgreSQL performs a fast shutdown with a
    shutdown checkpoint, and the grants are still there afterwards.
    """
    subprocess.run(["docker", "stop", "-t", str(timeout_s), name],
                    capture_output=True, text=True, timeout=timeout_s + 30)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=30)


def _restart_normal_mode(profile: Profile, image: str) -> None:
    """Restart the container without the dump mount and with normal-operation parameters."""
    _stop_container_gracefully(profile.container_name)
    admin_pw = profile.admin_password(create_if_managed=True)
    cmd = [
        "docker", "run", "-d", "--name", profile.container_name,
        "-v", f"{profile.volume_name}:/var/lib/postgresql/data",
        "-p", f"127.0.0.1:{profile.port}:5432",
        "-e", f"POSTGRES_PASSWORD={admin_pw}",
        "-e", f"POSTGRES_DB={profile.database}",
        image,
        *_NORMAL_TUNING,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RestoreError(redact(f"Restart into normal mode failed: {proc.stderr.strip()[:500]}", admin_pw))
    if not _wait_pg_isready(profile, timeout_s=90):
        raise RestoreError("PostgreSQL did not come up in time after restarting into normal mode.")


def _ensure_role_nologin(profile: Profile, admin_pw: str, role: str) -> None:
    sql = (
        f"DO $$ BEGIN\n"
        f"  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"    CREATE ROLE {role} NOLOGIN;\n"
        f"  END IF;\n"
        f"END $$;\n"
    )
    _docker_exec_psql_stdin(profile, admin_pw, sql)


def _run_pg_restore(profile: Profile, admin_pw: str, state: RestoreState,
                     emit: Callable[[RestoreState], RestoreState]) -> RestoreState:
    cmd = [
        "docker", "exec", "-e", f"PGPASSWORD={admin_pw}", profile.container_name,
        "pg_restore", "-U", profile.admin_user, "-d", profile.database,
        "-j", "4", "--no-acl", "--verbose", "/dump/db.backup",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    assert proc.stdout is not None
    try:
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            state = update_state_with_line(state, line)
            state = emit(state)
    finally:
        returncode = proc.wait()
    if returncode != 0:
        tail = "\n".join(state.log_tail[-10:])
        raise RestoreError(
            f"pg_restore exited with an error (exit code {returncode}). The volume is now "
            "half-filled and NOT resumable -- repeat the restore with --force to discard "
            f"volume and container and start over. Last lines:\n{redact(tail, admin_pw)}"
        )
    return state


def _post_processing(profile: Profile, admin_pw: str) -> None:
    _docker_exec_psql_stdin(profile, admin_pw, "ANALYZE;\n")

    role = profile.user or "explorer_ro"
    ro_pw = profile.resolve_password(create_if_managed=True)
    if not ro_pw:
        raise RestoreError("No password available for the read-only role.")

    setup_sql = f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
    CREATE ROLE {role} LOGIN;
  END IF;
END $$;
\\getenv cib7_ro_pw CIB7_RO_PW
ALTER ROLE {role} PASSWORD :'cib7_ro_pw';
ALTER ROLE {role} SET default_transaction_read_only = on;
ALTER ROLE {role} SET statement_timeout = '30s';
ALTER ROLE {role} SET idle_in_transaction_session_timeout = '60s';
GRANT CONNECT ON DATABASE {profile.database} TO {role};
GRANT USAGE ON SCHEMA public TO {role};
GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role};
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
"""
    _docker_exec_psql_stdin(profile, admin_pw, setup_sql, extra_env={"CIB7_RO_PW": ro_pw})
    _verify_read_only_access(profile, role, ro_pw)


def _verify_read_only_access(profile: Profile, role: str, ro_pw: str) -> None:
    """Prove that the read-only role can actually read -- rather than assuming it.

    Without this check a restore can report "finished" and leave behind a role that may log in
    but sees no table (see ``_stop_container_gracefully``). The failure would then surface only
    in the interface, as "permission denied" on every query. So at the end of a restore, one read
    is performed with the read-only role's own credentials.
    """
    core = "act_hi_procinst"
    cmd = ["docker", "exec", "-e", f"PGPASSWORD={ro_pw}", profile.container_name,
           "psql", "-v", "ON_ERROR_STOP=1", "-U", role, "-d", profile.database, "-tAc",
           f"SELECT 1 FROM {core} LIMIT 1"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RestoreError(redact(
            f"The restore completed, but the read-only role '{role}' cannot read {core}: "
            f"{proc.stderr.strip()[:300]}. Repeat the restore with --force.", ro_pw))
    log.info("read-only role '%s' can read -- the restore is usable", role)


# --- discarding and repeating -----------------------------------------------------------------

def reset(profile: Profile) -> None:
    """Discard container, volume and state file -- the only way to repeat an aborted or no longer
    matching restore. The dump file itself is left untouched."""
    subprocess.run(["docker", "rm", "-f", profile.container_name],
                    capture_output=True, text=True, timeout=30)
    subprocess.run(["docker", "volume", "rm", "-f", profile.volume_name],
                    capture_output=True, text=True, timeout=30)
    path = _state_path_for(profile.name)
    if path.exists():
        path.unlink()


# --- status() -----------------------------------------------------------------------------

def status(profile: Profile) -> RestoreState:
    """``read_state`` plus a reality check: is the container running, is the database reachable,
    do the core tables hold data, and does the fingerprint match? If so, the state is raised to
    READY (``adopted_existing=True``) and persisted."""
    prior = read_state(profile)
    avail, _msg = docker_available()
    if not avail:
        return prior

    if not _container_running(profile.container_name):
        return prior

    if not _pg_isready(profile):
        return prior

    if not _core_tables_present(profile):
        return prior

    dump_path = Path(profile.dump_file) if profile.dump_file else None
    current_fp = prior.dump_fingerprint
    if dump_path is not None and dump_path.is_file():
        try:
            current_fp = dump_fingerprint(dump_path)
        except OSError:
            pass

    if prior.dump_fingerprint and prior.dump_fingerprint != current_fp:
        # A database is there, but it demonstrably belongs to a different dump file.
        state = replace(
            prior,
            message=(
                "Found a running container, but its fingerprint does not match the current "
                "dump file -- not adopted automatically."
            ),
        )
        _save_state(state)
        return state

    state = replace(
        prior,
        phase=RestorePhase.READY,
        adopted_existing=True,
        dump_fingerprint=current_fp,
        toc_items_done=prior.toc_items_total or prior.toc_items_done,
        message="Adopted an existing container whose data matches.",
        finished_at=prior.finished_at or datetime.now(timezone.utc),
        error="",
    )
    _save_state(state)
    return state


# --- ensure_ready() -- the main path --------------------------------------------------------

def ensure_ready(
    profile: Profile,
    *,
    progress: Callable[[RestoreState], None] | None = None,
    force: bool = False,
) -> RestoreState:
    """The idempotent main path: make sure a profile has a ready, readable database -- either by
    adopting a matching existing one or by performing a full restore.

    Every state transition is written to disk immediately; that file *is* the progress display.
    The ``progress`` callback is only an additional hook.
    """

    def emit(s: RestoreState) -> RestoreState:
        _save_state(s)
        if progress is not None:
            try:
                progress(s)
            except Exception:  # noqa: BLE001 -- a progress callback must not break the restore
                log.exception("progress() callback failed")
        return s

    if force:
        reset(profile)

    state = read_state(profile)
    state = replace(
        state,
        phase=RestorePhase.CHECKING,
        message="checking Docker, the dump file and any existing data ...",
        started_at=state.started_at or datetime.now(timezone.utc),
        error="",
    )
    state = emit(state)

    try:
        avail, msg = docker_available()
        if not avail:
            raise RestoreError(f"Docker is not available: {msg}")

        dump_path = Path(profile.dump_file) if profile.dump_file else None
        if dump_path is None or not dump_path.is_file():
            raise RestoreError(f"Dump file is not readable: {profile.dump_file}")

        probe = probe_dump(dump_path)
        current_fp = dump_fingerprint(dump_path)
        prior_fp = state.dump_fingerprint

        state = replace(
            state,
            dump_size_bytes=dump_path.stat().st_size,
            dump_fingerprint=current_fp,
            toc_items_total=probe.get("toc_items_total"),
            source_server_version=probe.get("source_server_version"),
        )
        state = emit(state)

        # -- step 1: can an existing database be adopted? --------------------------------
        if not force and _container_running(profile.container_name):
            if _pg_isready(profile) and _core_tables_present(profile):
                if not prior_fp or prior_fp == current_fp:
                    state = replace(
                        state,
                        phase=RestorePhase.READY,
                        adopted_existing=True,
                        toc_items_done=state.toc_items_total or state.toc_items_done,
                        message="Adopted an existing container whose data matches.",
                        finished_at=datetime.now(timezone.utc),
                        error="",
                    )
                    return emit(state)
                raise RestoreError(
                    "A container is already running, but its stored fingerprint does not match "
                    "the current dump file. Repeat the restore with --force to discard volume "
                    "and container and load the dump again."
                )

        image = _pick_restore_image(profile, probe)

        # -- step 2: create container and volume -----------------------------------------
        state = replace(state, phase=RestorePhase.CREATING_CONTAINER,
                         message=f"creating container from image '{image}' ...")
        state = emit(state)
        admin_pw = _create_container(profile, image, force=force)

        # -- step 3: the restore itself --------------------------------------------------
        state = replace(state, phase=RestorePhase.RESTORING,
                         message="preparing role 'camunda' ...",
                         toc_items_done=0, tables_done=[], current_item="", log_tail=[])
        state = emit(state)
        _ensure_role_nologin(profile, admin_pw, _ROLE_CAMUNDA)

        state = replace(state, message="restore running (pg_restore) ...")
        state = emit(state)
        state = _run_pg_restore(profile, admin_pw, state, emit)

        # -- step 4: post-processing -----------------------------------------------------
        # Order matters: back to normal operation (fsync=on) FIRST, then the post-processing.
        # Otherwise the ANALYZE statistics and the read-only role's grants would be written
        # under fsync=off and can be lost when the mode changes.
        state = replace(state, phase=RestorePhase.POST_PROCESSING,
                         message="switching container to normal-operation parameters ...")
        state = emit(state)
        _restart_normal_mode(profile, image)

        state = replace(state, message="running ANALYZE and setting up the read-only role ...")
        state = emit(state)
        _post_processing(profile, admin_pw)

        # -- step 5: done ----------------------------------------------------------------
        state = replace(
            state,
            phase=RestorePhase.READY,
            adopted_existing=False,
            # Not every TOC entry produces a "finished item" line, so without this the
            # counter would stop short of 100 % on a successful restore.
            toc_items_done=state.toc_items_total or state.toc_items_done,
            source_server_version=probe.get("source_server_version"),
            dump_fingerprint=current_fp,
            message="restore complete.",
            finished_at=datetime.now(timezone.utc),
            error="",
        )
        return emit(state)

    except RestoreError as exc:
        state = replace(state, phase=RestorePhase.FAILED, error=redact(str(exc)))
        return emit(state)
    except Exception as exc:  # noqa: BLE001 -- never let an unredacted message escape
        log.exception("unexpected error during restore for profile '%s'", profile.name)
        state = replace(state, phase=RestorePhase.FAILED, error=redact(f"Unexpected error: {exc}"))
        return emit(state)


# --- background execution --------------------------------------------------------------------

_active_lock = threading.Lock()
_active_threads: dict[str, threading.Thread] = {}


def start_background(profile: Profile, *, force: bool = False) -> RestoreState:
    """Start ``ensure_ready`` in a daemon thread and return immediately.

    A second call while a run is already active for the same profile raises ``RestoreError``
    instead of letting two restores fight over the same volume.
    """
    with _active_lock:
        existing = _active_threads.get(profile.name)
        if existing is not None and existing.is_alive():
            raise RestoreError(
                f"A restore is already running in the background for profile '{profile.name}'."
            )

        def _run() -> None:
            try:
                ensure_ready(profile, force=force)
            except Exception:  # noqa: BLE001 -- ensure_ready already catches; this is the last net
                log.exception("background restore for profile '%s' aborted unexpectedly", profile.name)
            finally:
                with _active_lock:
                    _active_threads.pop(profile.name, None)

        thread = threading.Thread(target=_run, name=f"cib7-restore-{profile.name}", daemon=True)
        _active_threads[profile.name] = thread
        thread.start()

    return read_state(profile)
