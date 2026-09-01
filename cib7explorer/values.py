"""Value mode and allowlist.

Variable values are the most interesting part of process history and the most sensitive. So the
program does not decide whether they are displayed -- the connection profile and an optional
allowlist do:

| Classification | Allowlist | Result |
|---|---|---|
| test | none | all values (configured deliberately) |
| test | present | only what it names |
| unknown/prod | none | **no values** |
| unknown/prod | present | only what it names |

The definitions and catalogue views are unaffected by all of this and never show values at all;
that is enforced in ``db/varcatalog.py`` rather than merely intended.

Large and binary values are never loaded automatically. They exist only on explicit request and
with an upper bound, because a browser tab is not a download tool and a single 14 MB value can
otherwise stall a page for everyone.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import yaml

from .config import Profile
from .contracts import Classification

log = logging.getLogger("cib7explorer.values")

#: Without an explicit request, no value larger than this is loaded. Real installations hold
#: single values in the megabytes; loading one into a table row helps nobody.
AUTO_LOAD_MAX_BYTES = 4096

#: Not more than this even on request -- a view is not a download tool.
REQUEST_MAX_BYTES = 1_000_000


class ValuePolicy(str, Enum):
    ALL = "all"                  # test profile without an allowlist
    ALLOWLIST = "allowlist"      # only what the list names
    NONE = "none"                # no values

    @property
    def label(self) -> str:
        return {
            ValuePolicy.ALL: "all values (test profile without an allowlist)",
            ValuePolicy.ALLOWLIST: "allowlisted variables only",
            ValuePolicy.NONE: "no values",
        }[self]


@dataclass(frozen=True)
class Allowlist:
    """Released variables per process definition.

    YAML format, deliberately shaped so that it can be written straight out of the variable
    catalogue -- which is the only place that says which variables exist at all::

        allow:
          - definition: order-8000        # exact or a pattern (fnmatch), '*' means all
            variables: [orderNumber, customerNumber]
          - definition: "quote-*"
            variables: ["*Number"]

    Anything not named here is not shown, not even when the value mode is on.
    """

    entries: tuple[tuple[str, tuple[str, ...]], ...] = ()
    source: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> "Allowlist":
        p = Path(path).expanduser()
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        entries: list[tuple[str, tuple[str, ...]]] = []
        for raw in doc.get("allow") or []:
            definition = str(raw.get("definition", "*"))
            variables = tuple(str(v) for v in (raw.get("variables") or ["*"]))
            entries.append((definition, variables))
        return cls(entries=tuple(entries), source=str(p))

    def allows(self, def_key: str | None, name: str) -> bool:
        for definition, variables in self.entries:
            if def_key is not None and not fnmatch.fnmatch(def_key, definition):
                continue
            if def_key is None and definition != "*":
                continue
            if any(fnmatch.fnmatch(name, pattern) for pattern in variables):
                return True
        return False

    def __bool__(self) -> bool:
        return bool(self.entries)


@dataclass(frozen=True)
class ValueAccess:
    """The resolved decision for one profile -- the single place every view asks."""

    policy: ValuePolicy
    allowlist: Allowlist | None = None
    reason: str = ""

    def allows(self, def_key: str | None, name: str) -> bool:
        if self.policy is ValuePolicy.NONE:
            return False
        if self.policy is ValuePolicy.ALL:
            return True
        return bool(self.allowlist and self.allowlist.allows(def_key, name))

    def why_not(self, def_key: str | None, name: str) -> str:
        if self.allows(def_key, name):
            return ""
        if self.policy is ValuePolicy.NONE:
            return self.reason or "Value mode is off."
        return (f"'{name}' is not in the allowlist"
                + (f" ({self.allowlist.source})" if self.allowlist and self.allowlist.source else ""))


def resolve_access(profile: Profile) -> ValueAccess:
    """Derive the decision from the profile. The only place this happens."""
    allowlist: Allowlist | None = None
    if profile.values_allowlist_file:
        try:
            allowlist = Allowlist.load(profile.values_allowlist_file)
        except Exception as exc:  # noqa: BLE001 -- an unreadable list must not open the gate
            return ValueAccess(
                ValuePolicy.NONE,
                reason=(f"Allowlist {profile.values_allowlist_file} cannot be read "
                        f"({exc.__class__.__name__}). No values are shown, to be safe."))

    if not profile.values_mode_effective:
        return ValueAccess(ValuePolicy.NONE, allowlist,
                           reason=(profile.values_mode_locked_reason
                                   or "Value mode is switched off for this profile."))
    if allowlist:
        return ValueAccess(ValuePolicy.ALLOWLIST, allowlist,
                           reason=f"Allowlist from {allowlist.source}")
    if profile.classification is Classification.TEST:
        return ValueAccess(ValuePolicy.ALL, None,
                           reason=("Test profile without an allowlist -- deliberate here, and "
                                   "not acceptable on a production system."))
    return ValueAccess(ValuePolicy.NONE, None,
                       reason=("Without an allowlist, no values are shown for a profile that is "
                               "not classified as test."))


def write_example_allowlist(path: str | Path, entries: Iterable[tuple[str, Iterable[str]]] = ()
                            ) -> Path:
    """Write an allowlist in the format ``Allowlist.load`` reads.

    Meant to be seeded from the variable catalogue, which is where the names and their spread
    across definitions can actually be looked up.
    """
    p = Path(path).expanduser()
    doc = {"allow": [{"definition": d, "variables": list(v)} for d, v in entries]}
    header = (
        "# Allowlist for variable values.\n"
        "# Anything not named here is not shown, not even while the value mode is on.\n"
        "# 'definition' and 'variables' both understand patterns (*, ?).\n"
        "# The variable catalogue is the natural starting point for this file.\n")
    p.write_text(header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return p
