"""The only way to the database.

After ``connect()``, nothing above this layer knows where the connection came from -- a restored
dump, a database reachable directly, or an SSH tunnel later on.

Three properties are not negotiable here:

* **Read-only, provably.** A select-only role, ``default_transaction_read_only``, every query
  inside a transaction explicitly opened READ ONLY, plus the statement guard in ``sqlguard``.
* **A statement timeout and a row limit on every query**, with a visible message when either
  one was hit. An exploration tool that can hang a shared database is not usable twice.
* **No credentials in logs or error messages.**
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

import psycopg
from psycopg import sql as _sql  # noqa: F401  (re-exported for callers that must quote identifiers)
from psycopg.rows import tuple_row

from ..config import Profile, redact
from ..contracts import QueryResult
from . import sqlguard

log = logging.getLogger("cib7explorer.db")

# Session parameters set on every connection.
_SESSION_SETTINGS = {
    "application_name": "cib7explorer",
    "default_transaction_read_only": "on",
    "timezone": "UTC",              # keeps now() and comparisons deterministic
    "idle_in_transaction_session_timeout": "60000",
    "lock_timeout": "5000",
}


class DatabaseError(RuntimeError):
    """A database access failure -- the message has already been redacted."""


class QueryTimeout(DatabaseError):
    def __init__(self, timeout_ms: int, sql_head: str) -> None:
        super().__init__(
            f"Query cancelled after {timeout_ms} ms (statement timeout). Starts with: {sql_head}"
        )
        self.timeout_ms = timeout_ms


class NotReadOnly(DatabaseError):
    """The connection is not provably read-only, so no work continues on it."""


def _connect_timeout() -> int:
    """How long a connection attempt may take, in seconds.

    Configurable, because the right number depends on the environment rather than on the tool:
    through a tunnel it may take longer, and in a test suite an unreachable target should give
    up immediately. Hard-coding it once cost a test suite two and a half minutes of pure waiting
    per run.
    """
    import os

    raw = (os.environ.get("CIB7_CONNECT_TIMEOUT") or "").strip()
    if not raw:
        return 10
    try:
        value = int(raw)
    except ValueError:
        raise DatabaseError(f"CIB7_CONNECT_TIMEOUT='{raw}' is not a number of seconds.") from None
    if value < 1:
        raise DatabaseError("CIB7_CONNECT_TIMEOUT must be at least 1 second.")
    return value


@dataclass(frozen=True)
class ReadOnlyProof:
    """Evidence that the connection cannot write -- gathered without attempting a write.

    A test write would be the more direct proof, and it is exactly the code path that must not
    exist in this tool. So session flags and actual table privileges are queried instead. The
    distinction matters in the interface: "read-only is enforced" and "this role could not write
    even if it tried" are two different statements, and users deserve to see which one holds.
    """

    transaction_read_only: bool
    default_transaction_read_only: bool
    is_superuser: bool
    can_insert: bool
    can_update: bool
    can_delete: bool
    probed_table: str

    @property
    def ok(self) -> bool:
        return self.transaction_read_only and self.default_transaction_read_only

    @property
    def privileges_clean(self) -> bool:
        return not (self.can_insert or self.can_update or self.can_delete or self.is_superuser)

    @property
    def summary(self) -> str:
        if not self.ok:
            return "session is NOT set to read-only"
        if self.privileges_clean:
            return "read-only enforced, role holds no write privileges"
        detail = []
        if self.is_superuser:
            detail.append("superuser")
        for name, val in (("INSERT", self.can_insert), ("UPDATE", self.can_update),
                          ("DELETE", self.can_delete)):
            if val:
                detail.append(name)
        return ("read-only enforced, but the role would be allowed to write ("
                + ", ".join(detail) + ") -- a select-only role would be better")


class Database:
    """Connection holder for one profile. Thread-safe through a small pool."""

    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self._password = profile.resolve_password()
        self._pool: Any = None
        self._read_only_proof: ReadOnlyProof | None = None

    # -- connecting --------------------------------------------------------------------------

    def _conninfo(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "host": self.profile.host,
            "port": self.profile.port,
            "dbname": self.profile.database,
            "user": self.profile.user,
            "connect_timeout": _connect_timeout(),
        }
        if self._password:
            info["password"] = self._password
        if self.profile.sslmode:
            info["sslmode"] = self.profile.sslmode
        return info

    def _configure(self, conn: psycopg.Connection) -> None:
        conn.autocommit = True
        # SET takes no parameters, set_config() does -- so the value stays a parameter and is
        # never interpolated into SQL.
        with conn.cursor() as cur:
            for key, value in _SESSION_SETTINGS.items():
                cur.execute("SELECT set_config(%s, %s, false)", (key, value))
            cur.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (str(self.profile.statement_timeout_ms),),
            )
            # Queries name tables unqualified (``act_hi_procinst``) because they have to run
            # against installations that place the engine in different schemas -- ``public``
            # in a restored dump, a named schema where a schema-per-service convention
            # applies. The search path therefore comes from the profile and is set per
            # connection, rather than from a role default that would live inside somebody
            # else's database and be invisible from here. ``pg_catalog`` is always implicitly
            # first in PostgreSQL and is not listed.
            cur.execute(
                "SELECT set_config(%s, %s, false)",
                ("search_path", self.profile.schema),
            )

    def open(self) -> "Database":
        from psycopg_pool import ConnectionPool

        if self._pool is not None:
            return self
        try:
            self._pool = ConnectionPool(
                kwargs=self._conninfo(),
                min_size=1,
                max_size=self.profile.pool_max_size,
                configure=self._configure,
                open=True,
                timeout=_connect_timeout(),
                name=f"cib7-{self.profile.name}",
            )
            self._pool.wait(timeout=_connect_timeout() * 2)
        except Exception as exc:  # noqa: BLE001 -- every failure becomes one readable message
            self._pool = None
            raise DatabaseError(self._clean(exc)) from None
        self._read_only_proof = self._prove_read_only()
        if not self._read_only_proof.ok:
            self.close()
            raise NotReadOnly(
                "The connection could not be pinned to read-only. Nothing is queried while "
                "that is uncertain."
            )
        return self

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def __enter__(self) -> "Database":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- error handling ----------------------------------------------------------------------

    def _clean(self, exc: BaseException) -> str:
        text = str(exc) or exc.__class__.__name__
        return redact(text, self._password, self.profile.password_env)

    # -- the core: one read query ------------------------------------------------------------

    @contextmanager
    def _read_only_tx(self, timeout_ms: int) -> Iterator[psycopg.Connection]:
        if self._pool is None:
            self.open()
        assert self._pool is not None
        with self._pool.connection() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT set_config('statement_timeout', %s, true)",
                                (str(timeout_ms),))
                yield conn
            finally:
                conn.rollback()          # never commit -- there is nothing to commit
                conn.autocommit = True

    def fetch(
        self,
        sql: str,
        params: Sequence[Any] | dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        timeout_ms: int | None = None,
        name: str = "",
    ) -> QueryResult:
        """Run a read query.

        ``limit`` bounds the number of rows fetched (default: the profile's row limit). It is
        deliberately not written into the SQL but applied on the server side: the query runs in
        a named cursor and only ``limit + 1`` rows are fetched. That extra row is the evidence
        that truncation happened, which is what ``QueryResult.truncated`` reports -- instead of
        silently returning a shortened answer that looks complete.
        """
        checked = sqlguard.check(sql)
        row_limit = self.profile.row_limit if limit is None else limit
        tmo = timeout_ms or self.profile.statement_timeout_ms
        started = time.perf_counter()
        cursor_name = f"cib7_{name or 'q'}_{int(started * 1e6) % 1_000_000}"

        try:
            with self._read_only_tx(tmo) as conn:
                with conn.cursor(name=cursor_name, row_factory=tuple_row, scrollable=False) as cur:
                    cur.itersize = min(max(row_limit, 1), 10_000)
                    cur.execute(checked, params)
                    rows = cur.fetchmany(row_limit + 1) if row_limit > 0 else cur.fetchall()
                    cols = [d.name for d in (cur.description or [])]
        except psycopg.errors.QueryCanceled:
            raise QueryTimeout(tmo, " ".join(checked.split())[:120]) from None
        except psycopg.Error as exc:
            raise DatabaseError(self._clean(exc)) from None

        truncated = row_limit > 0 and len(rows) > row_limit
        if truncated:
            rows = rows[:row_limit]
        duration_ms = int((time.perf_counter() - started) * 1000)
        if duration_ms > 2000:
            log.info("slow query %s: %d ms", name or cursor_name, duration_ms)
        return QueryResult(
            columns=cols,
            rows=list(rows),
            truncated=truncated,
            limit=row_limit or None,
            duration_ms=duration_ms,
            statement_timeout_ms=tmo,
        )

    def scalar(self, sql: str, params: Any = None, *, timeout_ms: int | None = None) -> Any:
        res = self.fetch(sql, params, limit=1, timeout_ms=timeout_ms)
        return res.rows[0][0] if res.rows else None

    def one(self, sql: str, params: Any = None, *, timeout_ms: int | None = None) -> dict[str, Any] | None:
        return self.fetch(sql, params, limit=1, timeout_ms=timeout_ms).one

    # -- read-only proof ---------------------------------------------------------------------

    def _prove_read_only(self) -> ReadOnlyProof:
        probe = "act_hi_procinst"
        sql = """
            SELECT current_setting('transaction_read_only')                     AS tx_ro,
                   current_setting('default_transaction_read_only')             AS def_ro,
                   (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super,
                   CASE WHEN to_regclass(%(t)s) IS NULL THEN NULL
                        ELSE has_table_privilege(%(t)s, 'INSERT') END           AS can_ins,
                   CASE WHEN to_regclass(%(t)s) IS NULL THEN NULL
                        ELSE has_table_privilege(%(t)s, 'UPDATE') END           AS can_upd,
                   CASE WHEN to_regclass(%(t)s) IS NULL THEN NULL
                        ELSE has_table_privilege(%(t)s, 'DELETE') END           AS can_del
        """
        assert self._pool is not None
        try:
            with self._pool.connection() as conn:
                conn.autocommit = False
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(sql, {"t": probe})
                    row = cur.fetchone()
                conn.rollback()
                conn.autocommit = True
        except psycopg.Error as exc:
            raise DatabaseError(self._clean(exc)) from None
        tx_ro, def_ro, is_super, can_ins, can_upd, can_del = row  # type: ignore[misc]
        return ReadOnlyProof(
            transaction_read_only=(tx_ro == "on"),
            default_transaction_read_only=(def_ro == "on"),
            is_superuser=bool(is_super),
            can_insert=bool(can_ins),
            can_update=bool(can_upd),
            can_delete=bool(can_del),
            probed_table=probe,
        )

    @property
    def read_only_proof(self) -> ReadOnlyProof:
        if self._read_only_proof is None:
            raise DatabaseError("connection is not open")
        return self._read_only_proof


def connect(profile: Profile) -> Database:
    """The only public entry point -- behind it, the route to the data disappears."""
    return Database(profile).open()
