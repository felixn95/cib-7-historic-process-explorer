"""Shared fixtures for the test suite.

Ground rule: **no test outside `-m integration` may touch the developer's environment.** Not
their profiles file, not their state directory, and certainly not the network. That was once
different: the unit tests read `~/.config/cib7-explorer/profiles.yaml` and ran on exactly one
machine. It only came to light while renaming a profile, which is the worst way to find out.
Hence `_isolate_environment`.

The `integration` marker is registered in pyproject.toml (`-m integration` selects only those
tests). They need a reachable engine database; which profile is used comes from
`CIB7_TEST_PROFILE`. When the profile is undefined or the database unreachable, integration tests
skip themselves instead of failing -- that is a missing local environment, not a defect.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest


def _test_profile_name() -> str:
    return os.environ.get("CIB7_TEST_PROFILE", "demo-dump")


@pytest.fixture(scope="session")
def db() -> Iterator["object"]:
    """Open the database from the test profile once per run.

    Skips with a clear message rather than failing when there is no profile or no reachable
    database -- so `pytest -m integration` on a machine without a running container is a skip, not
    a red test run.
    """
    from cib7explorer import config
    from cib7explorer.db import connection

    name = _test_profile_name()
    try:
        profile = config.get_profile(name)
    except KeyError as exc:
        pytest.skip(f"test profile '{name}' is not defined ({exc}) -- integration test skipped.")
        return

    try:
        database = connection.connect(profile)
    except Exception as exc:  # noqa: BLE001 -- any connection failure leads to a skip
        pytest.skip(f"database for profile '{name}' unreachable ({exc}) -- integration test skipped.")
        return

    try:
        yield database
    finally:
        database.close()


@pytest.fixture(scope="session")
def profile():
    """The profile belonging to the `db` fixture -- separate, because many queries need it for
    the time zone, the guard rails and the editable pattern lists."""
    from cib7explorer import config

    name = _test_profile_name()
    try:
        return config.get_profile(name)
    except KeyError as exc:
        pytest.skip(f"test profile '{name}' is not defined ({exc}) -- integration test skipped.")


@pytest.fixture(scope="session")
def catalog(db, profile):
    """The variable catalogue, built once per run.

    The build scans `act_hi_varinst` several times. Tests that each build it themselves would
    scan that table a dozen times and get in each other's way -- in one run that pushed the main
    query from under three seconds to over two minutes and made the test fail on the statement
    timeout. The code was not at fault; the test setup was.
    """
    from cib7explorer.db import varcatalog

    return varcatalog.build_catalog(db, profile)


@pytest.fixture(scope="session")
def landscape(db, profile):
    """The process landscape, built once per run -- same reasoning as for the catalogue."""
    from cib7explorer.db import landscape as mod

    return mod.build_landscape(db, profile)


# --- subjects picked from the database ------------------------------------------------------
#
# The integration tests must not name process definitions, business keys or variables of any
# particular installation. Two reasons, and the second is the important one: hard-coded names
# make the suite unusable for everybody whose database is not the one it was written against,
# and they would carry data out of that database into this repository.
#
# So each of these fixtures asks the database for a suitable subject and skips when there is
# none. A test then states a property -- "the largest definition appears on the page" -- instead
# of a name.

@pytest.fixture(scope="session")
def busiest_def_key(db) -> str:
    """The process definition key with the most history rows."""
    key = db.scalar(
        "SELECT proc_def_key_ FROM act_hi_procinst "
        "WHERE proc_def_key_ IS NOT NULL "
        "GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1")
    if not key:
        pytest.skip("no process instance in the history")
    return str(key)


@pytest.fixture(scope="session")
def sample_business_key(db) -> str:
    """A business key that has more than one instance -- so a case has something to show."""
    key = db.scalar(
        "SELECT business_key_ FROM act_hi_procinst "
        "WHERE business_key_ IS NOT NULL AND business_key_ <> '' "
        "GROUP BY 1 HAVING count(*) > 1 ORDER BY count(*) DESC, 1 LIMIT 1")
    if not key:
        pytest.skip("no business key with more than one instance")
    return str(key)


@pytest.fixture(scope="session")
def sample_instance_id(db, sample_business_key) -> str:
    """A root instance of that case."""
    iid = db.scalar(
        "SELECT proc_inst_id_ FROM act_hi_procinst "
        "WHERE business_key_ = %s AND super_process_instance_id_ IS NULL LIMIT 1",
        (sample_business_key,))
    if not iid:
        iid = db.scalar("SELECT proc_inst_id_ FROM act_hi_procinst LIMIT 1")
    if not iid:
        pytest.skip("no process instance in the history")
    return str(iid)


@pytest.fixture(scope="session")
def variable_rich_instance_id(db) -> str:
    """The instance with the most variables.

    Several tests are about the variable table -- how many columns it has, that every value is
    clickable, that a copy button sits next to each. An instance with three variables would let
    all of them pass without proving anything, so the subject is chosen for the property the
    tests need.
    """
    iid = db.scalar(
        "SELECT proc_inst_id_ FROM act_hi_varinst "
        "WHERE proc_inst_id_ IS NOT NULL "
        "GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")
    if not iid:
        pytest.skip("no variable recorded in the history")
    return str(iid)


# --- isolating the unit tests ---------------------------------------------------------------

#: A profile that exists only in the tests.
#:
#: The target is `127.0.0.1:1` rather than an `.invalid` hostname: a page that does attempt a
#: connection should be refused **immediately**. With an unresolvable
#: name, every such attempt waits out the timeout -- that once turned a 3-second unit suite into a
#: 162-second one, seven tests idling 20 seconds each. Port 1 is privileged and nothing listens
#: there, so the connection fails in the first instant and always for the same reason.
DEMO_PROFILE = "demo-dump"

_DEMO_PROFILES_FILE = """profiles:
  - name: demo-dump
    kind: direct
    classification: test
    host: 127.0.0.1
    port: 1
    database: camunda
    user: reader
    password_env: CIB7_PW_DEMO
    source_timezone: UTC
    display_timezone: Europe/Berlin
"""


@pytest.fixture(autouse=True)
def _isolate_environment(request, tmp_path_factory, monkeypatch):
    """Point the profiles file, the state directory and the mark list at a throwaway directory.

    Applies to every test WITHOUT the `integration` marker. Integration tests need the real
    profile and its database, so they are left alone.

    Tests that set `CIB7_PROFILES` themselves still win: autouse fixtures run before the test
    body, and a `monkeypatch.setenv` in there overrides this default.
    """
    if request.node.get_closest_marker("integration"):
        return
    root = tmp_path_factory.mktemp("environment")
    profiles_file = root / "profiles.yaml"
    profiles_file.write_text(_DEMO_PROFILES_FILE, encoding="utf-8")
    monkeypatch.setenv("CIB7_PROFILES", str(profiles_file))
    monkeypatch.setenv("CIB7_CONFIG_DIR", str(root))
    monkeypatch.setenv("CIB7_STATE_DIR", str(root / "state"))
    monkeypatch.setenv("CIB7_NOTES", str(root / "marks.sqlite"))
    monkeypatch.setenv("CIB7_PW_DEMO", "for-the-test-only")
    # An unreachable target should give up immediately, not wait out the production timeout.
    monkeypatch.setenv("CIB7_CONNECT_TIMEOUT", "1")


@pytest.fixture
def demo_profile():
    """The test profile as an object -- for tests that only need the zone and the classification."""
    from cib7explorer import config

    return config.get_profile(DEMO_PROFILE)
