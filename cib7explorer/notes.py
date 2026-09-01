"""Marks: holding on to interesting cases.

Exploring a large history means finding a case worth remembering, following a thread away from
it, and then not being able to get back. This list is the remedy, and it is the seed for
whatever real investigation follows.

Hence a SQLite file of its own next to the tool rather than an entry in the cache: a cache is
disposable, these notes are not. Exportable as JSON and CSV. **No variable values**: a mark
holds references only -- business key, instance id, activity -- plus the user's own text and a
timestamp.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

from .config import notes_path, state_dir

log = logging.getLogger("cib7explorer.notes")


class MarkKind(str, Enum):
    BUSINESS_KEY = "business_key"
    INSTANCE = "instance"
    ACTIVITY = "activity"
    DEFINITION = "definition"

    @property
    def label(self) -> str:
        return {
            MarkKind.BUSINESS_KEY: "case",
            MarkKind.INSTANCE: "process instance",
            MarkKind.ACTIVITY: "activity",
            MarkKind.DEFINITION: "process definition",
        }[self]


@dataclass(frozen=True)
class Mark:
    id: int | None
    kind: MarkKind
    reference: str
    note: str = ""
    profile_name: str = ""
    installation_id: str | None = None
    context: str = ""            # e.g. the business key an instance belongs to
    created_at: str = ""

    @property
    def created_dt(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.created_at)
        except (TypeError, ValueError):
            return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mark (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    reference       TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    profile_name    TEXT NOT NULL DEFAULT '',
    installation_id TEXT,
    context         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mark_kind_ref ON mark (kind, reference);
"""


class Notes:
    """The mark list. Deliberately small: add, list, edit, delete, export."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else notes_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    # -- writing ---------------------------------------------------------------------------

    def add(self, kind: MarkKind | str, reference: str, note: str = "", *,
            profile_name: str = "", installation_id: str | None = None,
            context: str = "") -> Mark:
        kind = MarkKind(kind)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO mark (kind, reference, note, profile_name, installation_id, "
                "context, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind.value, reference, note, profile_name, installation_id, context, now))
            new_id = cur.lastrowid
        return Mark(id=new_id, kind=kind, reference=reference, note=note,
                    profile_name=profile_name, installation_id=installation_id,
                    context=context, created_at=now)

    def update_note(self, mark_id: int, note: str) -> bool:
        with self._connect() as con:
            cur = con.execute("UPDATE mark SET note = ? WHERE id = ?", (note, mark_id))
        return cur.rowcount > 0

    def remove(self, mark_id: int) -> bool:
        with self._connect() as con:
            cur = con.execute("DELETE FROM mark WHERE id = ?", (mark_id,))
        return cur.rowcount > 0

    # -- reading ---------------------------------------------------------------------------

    def _row(self, row: sqlite3.Row) -> Mark:
        return Mark(id=row["id"], kind=MarkKind(row["kind"]), reference=row["reference"],
                    note=row["note"], profile_name=row["profile_name"],
                    installation_id=row["installation_id"], context=row["context"],
                    created_at=row["created_at"])

    def all(self, *, kind: MarkKind | str | None = None, profile_name: str | None = None
            ) -> list[Mark]:
        sql = "SELECT * FROM mark"
        where, params = [], []
        if kind is not None:
            where.append("kind = ?")
            params.append(MarkKind(kind).value)
        if profile_name:
            where.append("profile_name = ?")
            params.append(profile_name)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC"
        with self._connect() as con:
            return [self._row(r) for r in con.execute(sql, params)]

    def for_reference(self, kind: MarkKind | str, reference: str) -> list[Mark]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM mark WHERE kind = ? AND reference = ? "
                               "ORDER BY created_at DESC",
                               (MarkKind(kind).value, reference))
            return [self._row(r) for r in rows]

    def count(self) -> int:
        with self._connect() as con:
            return int(con.execute("SELECT count(*) FROM mark").fetchone()[0])

    # -- export ----------------------------------------------------------------------------

    def to_json(self, marks: Iterable[Mark] | None = None) -> str:
        data = [
            {**asdict(m), "kind": m.kind.value, "kind_label": m.kind.label}
            for m in (marks if marks is not None else self.all())
        ]
        return json.dumps({"marks": data, "exported_at": datetime.now(timezone.utc)
                           .isoformat(timespec="seconds")},
                          indent=2, ensure_ascii=False)

    def to_csv(self, marks: Iterable[Mark] | None = None, *, delimiter: str = ";") -> str:
        """CSV with a semicolon delimiter and a BOM.

        Both are concessions to spreadsheet software: without the BOM, Excel reads UTF-8 as
        Latin-1, and in locales where the comma is the decimal separator it ignores a
        comma-delimited file's columns entirely. Pass ``delimiter=","`` for anything else.
        """
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
        writer.writerow(["Kind", "Reference", "Context", "Note", "Profile",
                         "Installation", "Created"])
        for m in (marks if marks is not None else self.all()):
            writer.writerow([m.kind.label, m.reference, m.context, m.note, m.profile_name,
                             m.installation_id or "", m.created_at])
        return "﻿" + buf.getvalue()


def default_notes() -> Notes:
    return Notes(notes_path())
