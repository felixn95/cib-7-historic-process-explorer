"""Static query screening -- defence in depth, not the primary safeguard.

The primary safeguard is the database itself: a role with select-only grants and
``default_transaction_read_only``. This guard adds a second, cheaper barrier: nothing but a
single read statement ever leaves the process. Two barriers matter here because they fail
differently -- a misconfigured role is a deployment mistake, a stray write statement is a
programming mistake, and neither one covers the other.

Error messages are deliberately specific about *what* was rejected. A guard that only says
"unsafe query" turns every false positive into a debugging session.
"""

from __future__ import annotations

import re


class UnsafeQuery(ValueError):
    """The query is not a single read statement."""


_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SINGLE_QUOTED = re.compile(r"'(?:[^']|'')*'")
_DOUBLE_QUOTED = re.compile(r'"(?:[^"]|"")*"')
_DOLLAR_QUOTED = re.compile(r"\$(\w*)\$.*?\$\1\$", re.DOTALL)

#: Rejected when they appear in the query text. Literals and quoted identifiers are blanked
#: out first, so a value such as ``proc_def_key_ = 'update-account'`` does not trip
#: the "create" rule.
_FORBIDDEN = (
    "insert", "update", "delete", "merge", "truncate", "drop", "alter", "create",
    "grant", "revoke", "copy", "vacuum", "analyze", "reindex", "cluster", "refresh",
    "lock", "call", "do", "set", "reset", "listen", "notify", "prepare", "execute",
    "declare", "discard", "import", "security", "nextval", "setval", "pg_read_file",
    "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export", "dblink", "pg_sleep",
    "pg_terminate_backend", "pg_cancel_backend", "pg_advisory_lock",
)

_ALLOWED_STARTS = ("select", "with", "table", "values", "explain")


def _blank_literals(sql: str) -> str:
    """Replace comments, string literals and quoted identifiers with placeholders."""
    out = _BLOCK_COMMENT.sub(" ", sql)
    out = _LINE_COMMENT.sub(" ", out)
    out = _DOLLAR_QUOTED.sub(" '' ", out)
    out = _SINGLE_QUOTED.sub(" '' ", out)
    out = _DOUBLE_QUOTED.sub(" ident ", out)
    return out


def check(sql: str) -> str:
    """Screen a query and return it without its trailing semicolon.

    Raises ``UnsafeQuery`` unless the input is exactly one read statement.
    """
    if not sql or not sql.strip():
        raise UnsafeQuery("empty query")

    stripped = _blank_literals(sql)

    # Exactly one statement: no semicolon except a trailing one.
    body, _, tail = stripped.rpartition(";")
    if body and tail.strip():
        raise UnsafeQuery("multiple statements in one query are not allowed")
    if ";" in (body or ""):
        raise UnsafeQuery("multiple statements in one query are not allowed")

    head = stripped.lstrip().lower()
    if not head.startswith(_ALLOWED_STARTS):
        first = (head.split() or ["?"])[0]
        raise UnsafeQuery(f"only read statements are allowed, found: '{first}'")

    words = set(re.findall(r"[a-z_][a-z_0-9]*", stripped.lower()))
    hits = sorted(words & set(_FORBIDDEN))
    if hits:
        raise UnsafeQuery(f"forbidden keywords in query: {', '.join(hits)}")

    if re.search(r"\bfor\s+(update|no\s+key\s+update|share|key\s+share)\b", stripped, re.I):
        raise UnsafeQuery("locking clauses (FOR UPDATE/SHARE) are not allowed")

    return sql.strip().rstrip(";").rstrip()
