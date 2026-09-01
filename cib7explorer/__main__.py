"""A small command line for the process explorer.

Deliberately thin: every command only calls into library code (config, restore, db, web), so this
layer carries no behaviour of its own that would then be missing everywhere else.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys

from . import config
from .config import Profile, ProfileKind
from .contracts import RestorePhase, RestoreState
from .restore import docker_restore

log = logging.getLogger("cib7explorer.cli")


# --- output helpers -------------------------------------------------------------------------

def _print_progress(state: RestoreState) -> None:
    pct = state.percent
    pct_txt = f"{pct:3d}%" if pct is not None else "  ? "
    detail = state.current_item or state.message
    print(f"[{state.phase.value:<18}] {pct_txt} {detail}", file=sys.stderr)


def _fail(msg: str) -> int:
    print(config.redact(f"Error: {msg}"), file=sys.stderr)
    return 1


# --- profiles -------------------------------------------------------------------------------

def cmd_profiles(args: argparse.Namespace) -> int:
    try:
        profiles = config.load_profiles()
    except Exception as exc:  # noqa: BLE001 -- a broken profiles file must not print a traceback
        return _fail(str(exc))

    if not profiles:
        print("No profiles found. 'cib7explorer init-profiles' writes an example.")
        return 0

    header = f"{'Name':<20} {'Kind':<14} {'Class':<10} {'Target':<55} {'Values'}"
    print(header)
    print("-" * len(header))
    for prof in profiles.values():
        if prof.kind is ProfileKind.LOCAL_RESTORE:
            target = f"dump: {prof.dump_file}"
        else:
            target = f"{prof.host}:{prof.port}/{prof.database}"
        values = "on" if prof.values_mode_effective else "off"
        print(f"{prof.name:<20} {prof.kind.value:<14} {prof.classification.value:<10} {target:<55} {values}")
    return 0


# --- init-profiles --------------------------------------------------------------------------

def cmd_init_profiles(args: argparse.Namespace) -> int:
    try:
        path = config.write_example_profiles(dump_file=args.dump)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc))
    print(f"Example profiles file written: {path}")
    return 0


# --- restore --------------------------------------------------------------------------------

def cmd_restore(args: argparse.Namespace) -> int:
    try:
        profile = config.get_profile(args.profile)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc))

    try:
        final = docker_restore.ensure_ready(profile, progress=_print_progress, force=args.force)
    except Exception as exc:  # noqa: BLE001 -- ensure_ready already catches nearly everything
        return _fail(str(exc))

    if final.phase is RestorePhase.READY:
        print(
            f"Ready. adopted_existing={final.adopted_existing} "
            f"source={final.source_server_version} tables_done={len(final.tables_done)}",
            file=sys.stderr,
        )
        return 0

    print(config.redact(f"Restore is not ready (phase {final.phase.value}): "
                        f"{final.error or final.message}"), file=sys.stderr)
    return 1


# --- restore-status -------------------------------------------------------------------------

def cmd_restore_status(args: argparse.Namespace) -> int:
    try:
        profile = config.get_profile(args.profile)
        state = docker_restore.status(profile)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc))

    print(f"Profile:          {state.profile_name}")
    print(f"Phase:            {state.phase.value}")
    print(f"Progress:         {state.percent}%" if state.percent is not None else "Progress:         unknown")
    print(f"Adopted existing: {state.adopted_existing}")
    print(f"Source version:   {state.source_server_version}")
    print(f"TOC entries:      {state.toc_items_done}/{state.toc_items_total}")
    print(f"Tables finished:  {len(state.tables_done)}")
    print(f"Current item:     {state.current_item}")
    print(f"Message:          {state.message}")
    if state.error:
        print(f"Error:            {config.redact(state.error)}")
    return 0


# --- detect ---------------------------------------------------------------------------------

def cmd_detect(args: argparse.Namespace) -> int:
    try:
        profile = config.get_profile(args.profile)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc))

    try:
        from .db import detect as detect_mod
    except ImportError:
        print("The detection module cannot be loaded — check the installation.")
        return 1

    try:
        from .db.connection import connect
        with connect(profile) as db:
            result = detect_mod.detect(db, profile)
    except AttributeError:
        print("The detection module cannot be loaded — check the installation.")
        return 1
    except Exception as exc:  # noqa: BLE001
        return _fail(f"during detection: {exc}")

    payload = dataclasses.asdict(result) if dataclasses.is_dataclass(result) else result
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


# --- serve ----------------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        return _fail("uvicorn is not installed.")

    try:
        from .web.app import app
    except (ImportError, RuntimeError) as exc:
        # RuntimeError also covers missing optional dependencies of the web module; for this CLI
        # both mean the same thing: it cannot be served right now.
        print(f"The web interface cannot be started: {config.redact(str(exc))}")
        return 1

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


# --- argparse / main ------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cib7explorer",
        description="Process explorer for CIB seven / Camunda 7 history -- command line",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("profiles", help="list the connection profiles")

    p_init = sub.add_parser("init-profiles", help="write an example profiles file")
    p_init.add_argument("--dump", default=None, help="path to the dump file for the example profile")

    p_restore = sub.add_parser("restore", help="run a dump restore in the foreground")
    p_restore.add_argument("profile", help="profile name")
    p_restore.add_argument("--force", action="store_true",
                           help="discard the existing container and volume and restore again")

    p_status = sub.add_parser("restore-status", help="show the restore state of a profile")
    p_status.add_argument("profile", help="profile name")

    p_detect = sub.add_parser("detect", help="connect and print the detection result")
    p_detect.add_argument("profile", help="profile name")

    p_serve = sub.add_parser("serve", help="start the web interface")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    return parser


_HANDLERS = {
    "profiles": cmd_profiles,
    "init-profiles": cmd_init_profiles,
    "restore": cmd_restore,
    "restore-status": cmd_restore_status,
    "detect": cmd_detect,
    "serve": cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _HANDLERS[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("Aborted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 -- last line of defence against a bare traceback
        print(config.redact(f"Unexpected error: {exc}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
