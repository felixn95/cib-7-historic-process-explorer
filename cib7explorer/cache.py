"""Local precomputation cache (SQLite).

The target database is read-only and must not be asked to store anything on our behalf.
Anything that is computed once and then displayed with a visible "as of" timestamp -- the
variable catalogue, the process landscape -- therefore lands in a local SQLite file next to the
tool rather than in the database being explored.

**Contract for callers, not a check in this class:** the cache must never contain variable
values from the target database. ``put()`` stores whatever it is handed without knowing whether
a value is hidden inside it. Keeping values out of a payload while the value mode is off is the
caller's job (typically ``cib7explorer.db``). This class cannot verify that and does not
pretend to.

The file name (see ``path``) contains the installation id -- or the profile name as a fallback
-- plus a short schema fingerprint. When the fingerprint changes, because the schema was
migrated or because a different installation now sits behind the same profile, a new file
appears instead of stale numbers quietly being served from the wrong cache.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import cache_dir, ensure_dirs
from .contracts import DetectionResult

#: Written into every cache's meta table -- purely informational, no compatibility check here.
TOOL_VERSION = "0.1.0"

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _sanitize(name: str) -> str:
    cleaned = _SANITIZE_RE.sub("-", (name or "").strip()) or "unnamed"
    return cleaned[:80]


class Cache:
    """A precomputation cache for exactly one (installation, schema state) combination."""

    def __init__(self, installation_id: str | None, schema_fingerprint: str, profile_name: str) -> None:
        self.installation_id = installation_id
        self.schema_fingerprint = schema_fingerprint
        self.profile_name = profile_name
        ensure_dirs()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @classmethod
    def for_detection(cls, profile_name: str, det: DetectionResult) -> "Cache":
        """Build a cache from a detection result -- the usual route."""
        fp = cls.compute_schema_fingerprint(det)
        return cls(det.installation_id, fp, profile_name)

    @staticmethod
    def compute_schema_fingerprint(det: DetectionResult) -> str:
        """Fingerprint over the *shape* of the schema -- never over instance data.

        ``DetectionResult`` has no field for this; it is derived from what detection has
        established anyway: engine schema version, database name, which tables exist, and which
        deviations were found. Change any of those and the fingerprint changes with them.
        """
        parts = [
            det.database_name or "",
            det.engine_schema_version or "",
            "|".join(sorted(f"{t.name}:{t.exists}" for t in det.tables)),
            "|".join(sorted(f"{d.table}:{d.kind}:{d.detail}" for d in det.deviations)),
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    @property
    def path(self) -> Path:
        stem = _sanitize(self.installation_id or self.profile_name)
        fp8 = hashlib.sha256((self.schema_fingerprint or "").encode("utf-8")).hexdigest()[:8]
        return cache_dir() / f"{stem}-{fp8}.sqlite"

    # -- SQLite plumbing -------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._session() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cache_entry ("
                "key TEXT PRIMARY KEY, payload TEXT, created_at TEXT, "
                "source_note TEXT, bytes INTEGER)"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT)")
            for name, value in (
                ("installation_id", self.installation_id or ""),
                ("schema_fingerprint", self.schema_fingerprint or ""),
                ("profile_name", self.profile_name),
                ("tool_version", TOOL_VERSION),
            ):
                conn.execute(
                    "INSERT INTO meta (name, value) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                    (name, value),
                )

    # -- public API ------------------------------------------------------------------------

    def put(self, key: str, payload: object, *, source_note: str = "") -> None:
        """Store a payload as JSON (``default=str``, so nested datetimes survive).

        Reminder: ``payload`` must not contain variable values from the target database -- see
        the class docstring.
        """
        text = json.dumps(payload, ensure_ascii=False, default=str)
        now = datetime.now(timezone.utc).isoformat()
        with self._session() as conn:
            conn.execute(
                "INSERT INTO cache_entry (key, payload, created_at, source_note, bytes) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
                "created_at=excluded.created_at, source_note=excluded.source_note, "
                "bytes=excluded.bytes",
                (key, text, now, source_note, len(text.encode("utf-8"))),
            )

    def get(self, key: str) -> tuple[object, datetime] | None:
        """Return ``(payload, created_at)`` or ``None`` when the key is absent.

        Dates *inside* the payload come back as strings -- JSON has no datetime. Only
        ``created_at`` itself is a real datetime object.
        """
        with self._session() as conn:
            row = conn.execute(
                "SELECT payload, created_at FROM cache_entry WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        created_at = datetime.fromisoformat(row[1])
        return payload, created_at

    def age(self, key: str) -> timedelta | None:
        """Age of an entry -- what the visible "as of ..." line in the views is built from."""
        entry = self.get(key)
        if entry is None:
            return None
        _, created_at = entry
        now = datetime.now(created_at.tzinfo) if created_at.tzinfo else datetime.now()
        return now - created_at

    def entries(self) -> list[dict]:
        """All entries without their payloads -- enough for a cache management view."""
        with self._session() as conn:
            rows = conn.execute(
                "SELECT key, created_at, bytes, source_note FROM cache_entry "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [
            {"key": r[0], "created_at": r[1], "bytes": r[2], "source_note": r[3]}
            for r in rows
        ]

    def drop(self, key: str | None = None) -> int:
        """Delete one entry, or all of them without a ``key`` -- what "rebuild" calls."""
        with self._session() as conn:
            if key is None:
                cur = conn.execute("DELETE FROM cache_entry")
            else:
                cur = conn.execute("DELETE FROM cache_entry WHERE key = ?", (key,))
            return cur.rowcount
