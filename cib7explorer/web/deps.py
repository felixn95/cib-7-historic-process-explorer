"""Small helpers for the web layer: load profiles, open and close connections, format numbers
and timestamps for display.

Connection strategy: **connect/close per request**, not a long-lived registry. The reasons:

* This is an exploration tool for a handful of people at a time. There is no concurrency
  pressure that would justify holding a pool across requests.
* ``Database.open()`` already builds a small ``psycopg_pool`` (min_size=1); reconnecting per
  request costs a few milliseconds against a local database or through a tunnel.
* A permanently open connection per profile would mean the web layer holds state that can go
  stale after a restore, a dropped tunnel or a profile change -- the exact opposite of being
  able to state honestly what is being read.
* Synchronous endpoints run in FastAPI's thread pool; short synchronous connect/query/close
  cycles are a simpler model there than a shared, cross-thread registry with its own locking.

Should this ever become a bottleneck, the obvious next step is a small TTL-based cache at the
``Database`` level -- not an architectural change, just this one function.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone as _dt_timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from .. import config
from ..config import Profile
from ..db import connection
from ..db.connection import Database

log = logging.getLogger("cib7explorer.web")


# --- Profile -----------------------------------------------------------------------------

def load_profiles() -> dict[str, Profile]:
    """Load all profiles. Returns an empty dict when the file is missing -- callers decide
    whether that is an error or something to explain on the page."""
    return config.load_profiles()


def profiles_file_missing() -> bool:
    return not config.profiles_path().exists()


def no_profile_configured() -> bool:
    """There is no target at all -- neither from the profiles file nor from the environment.

    A missing *file* alone is not a defect: in a container the environment describes the target
    (``CIB7_DB_HOST``) and there deliberately is no profiles file. Telling the user to "create a
    profiles file" would be wrong advice there.
    """
    return not load_profiles()


def get_profile_or_404(name: str) -> Profile:
    profs = load_profiles()
    if name not in profs:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Profile '{name}' is not known. It is not in {config.profiles_path()}. "
                "Available profiles: " + (", ".join(sorted(profs)) if profs else "none")
            ),
        )
    return profs[name]


# --- connections ---------------------------------------------------------------------------

@contextmanager
def open_database(profile: Profile) -> Iterator[Database]:
    """Open a connection for the duration of a ``with`` block and close it afterwards --
    see the module docstring for why there is no connection registry."""
    db = connection.connect(profile)
    try:
        yield db
    finally:
        db.close()


# --- Docker availability (for /health and the restore view) --------------------------------

def docker_available() -> tuple[bool, str]:
    """Whether Docker is reachable -- needed for the dump restore path.

    Imported lazily: the interface has to start even when the restore module is missing or Docker
    is not installed at all. The result is cached briefly over there, because ``docker info``
    runs into its timeout when the daemon is off.
    """
    try:
        from ..restore import docker_restore as rst
    except ImportError as exc:
        return False, f"The restore module cannot be loaded ({exc})"
    try:
        return rst.docker_available()
    except Exception as exc:  # noqa: BLE001 -- foreign module, return shape not guaranteed
        return False, f"Docker check failed: {exc}"


# --- numbers and timestamps ----------------------------------------------------------------

def fmt_int(n: int | float | None) -> str:
    """Thousands separators for readability. ``None`` is never invented as a zero."""
    if n is None:
        return "not determined"
    return f"{int(round(n)):,}"


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "not determined"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def to_display_tz(dt: datetime | None, profile: Profile) -> datetime | None:
    """Convert a timestamp into the profile's display zone.

    The assumption, and it matters: a timestamp without ``tzinfo`` comes raw from the engine and
    is expressed in the zone configured as ``source_timezone`` -- the zone of the JVM that wrote
    it. Not UTC, even though the session itself runs on UTC: that setting only governs how
    *psycopg* interprets ``timestamptz`` values, not what was historically stored in a naive
    ``timestamp`` column. Timestamps that do carry ``tzinfo`` (such as the ``detected_at`` values
    produced by detection itself) pass through unchanged.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(profile.source_timezone))
    return dt.astimezone(ZoneInfo(profile.display_timezone))


def fmt_dt(dt: datetime | None, profile: Profile, *, with_zone: bool = True) -> str:
    disp = to_display_tz(dt, profile)
    if disp is None:
        return "not determined"
    text = disp.strftime("%Y-%m-%d %H:%M:%S")
    if with_zone:
        text += f" {profile.display_timezone}"
    return text


def fmt_ago(seconds: float | None) -> str:
    if seconds is None:
        return "not determined"
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)} s ago"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)} min ago"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} h ago"
    return f"{hours / 24:.1f} days ago"


def now_utc() -> datetime:
    return datetime.now(_dt_timezone.utc)


# --- path prefix behind a proxy -------------------------------------------------------------

class _BasePath:
    """The path prefix as a Jinja global -- deliberately an object, not a string.

    Registered as a string, the value would be frozen when this module is *imported*. That bites
    exactly where it is expensive: in tests that set the prefix and then render, and in a process
    that learns its environment only later. An object whose ``__str__`` looks the value up on
    every render lets templates keep writing a plain ``{{ base_path }}/profile/...`` -- Jinja
    calls ``str()`` for ``{{ }}`` and for ``~`` alike.
    """

    def __str__(self) -> str:
        return config.base_path()

    def __repr__(self) -> str:  # keeps misuse in a template readable
        return str(self)


#: Registered as ``base_path`` in every Jinja environment (once per router, like static_version).
base_path = _BasePath()


class _AuthEnabled:
    """Whether a login is configured -- as a truth value for the templates.

    This is the only way ``base.html`` may touch ``request.session``: without the session
    middleware that access raises, and an interface that fails on its own header is worse than
    one that simply shows no user name.
    """

    def __bool__(self) -> bool:
        import os

        return bool((os.environ.get("CIB7_OIDC_ISSUER") or "").strip())


#: Registered as ``auth_enabled`` in every Jinja environment.
auth_enabled = _AuthEnabled()


def with_base_path(path: str) -> str:
    """Put the prefix in front of an absolute path -- for URLs built in Python."""
    return config.base_path() + path


def without_base_path(path: str) -> str:
    """The inverse of ``with_base_path``: the application-internal path, without the prefix.

    Anything that compares a path against this application's routes has to go through here
    first. The reason is easy to miss: the proxy strips the prefix, but Starlette reassembles
    ``request.url.path`` from ``root_path`` + ``path``. A comparison against "/health" then never
    matches, because the path reads "/process-explorer/health" -- which is exactly how the auth
    gatekeeper once locked out the container's own health check.
    """
    base = config.base_path()
    if base and (path == base or path.startswith(base + "/")):
        return path[len(base):] or "/"
    return path


_STATIC_DIR = Path(__file__).parent / "static"
_static_versions: dict[str, str] = {}


def static_version(filename: str) -> str:
    """Content hash of a static file, appended to its URL.

    Without it a browser keeps stale CSS or JavaScript and a fixed interface looks unchanged to
    the user -- a failure that is miserable to diagnose, because everything is correct on the
    server side.
    """
    if filename in _static_versions:
        return _static_versions[filename]
    path = _STATIC_DIR / filename
    try:
        import hashlib

        stamp = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    except OSError:
        stamp = "0"
    _static_versions[filename] = stamp
    return stamp
