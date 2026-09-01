"""The profile that the environment describes (``CIB7_DB_*``).

Why this file exists: the same application runs on a workstation against a restored dump and in
a container against the database of whatever environment it hangs in. What can go wrong there is
not the connection -- that is noticed immediately -- but the **classification**: a profile that
silently defaults to ``test`` shows variable values. On a shared server that would be a data
protection incident rather than a bug somebody stumbles over. So the default is under test here.
"""

from __future__ import annotations

import pytest

from cib7explorer import config
from cib7explorer.contracts import Classification, ProfileKind

ALL_VARIABLES = (
    "CIB7_DB_HOST", "CIB7_DB_PORT", "CIB7_DB_NAME", "CIB7_DB_SCHEMA", "CIB7_DB_USER",
    "CIB7_DB_PASSWORD", "CIB7_DB_SSLMODE", "CIB7_CLASSIFICATION", "CIB7_PROFILE_NAME",
    "CIB7_SOURCE_TZ", "CIB7_DISPLAY_TZ", "CIB7_VALUES_ALLOWLIST", "CIB7_VALUES_MODE",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """No test may depend on the developer machine's environment."""
    for name in ALL_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_without_db_host_there_is_no_environment_profile():
    # Running with a profiles file must not be disturbed by this capability existing.
    assert config.profile_from_env() is None


def test_db_host_is_enough_everything_else_has_defaults(monkeypatch):
    monkeypatch.setenv("CIB7_DB_HOST", "postgres")
    p = config.profile_from_env()
    assert p is not None
    assert (p.host, p.port, p.database, p.schema, p.user) == (
        "postgres", 5432, "camunda", "public", "explorer_ro")
    assert p.kind is ProfileKind.DIRECT          # nothing the tool manages itself
    assert p.name == "environment"


def test_without_a_classification_values_stay_hidden(monkeypatch):
    monkeypatch.setenv("CIB7_DB_HOST", "postgres")
    p = config.profile_from_env()
    assert p.classification is Classification.UNKNOWN
    assert p.values_mode_effective is False
    assert "no allowlist" in p.values_mode_locked_reason


def test_values_only_with_an_explicit_classification(monkeypatch):
    monkeypatch.setenv("CIB7_DB_HOST", "postgres")
    monkeypatch.setenv("CIB7_CLASSIFICATION", "test")
    assert config.profile_from_env().values_mode_effective is True

    monkeypatch.setenv("CIB7_CLASSIFICATION", "prod")
    p = config.profile_from_env()
    assert p.classification is Classification.PROD
    assert p.values_mode_effective is False      # prod without an allowlist: no values


def test_a_nonsense_classification_is_not_swallowed(monkeypatch):
    monkeypatch.setenv("CIB7_DB_HOST", "postgres")
    monkeypatch.setenv("CIB7_CLASSIFICATION", "productive")
    with pytest.raises(ValueError, match="CIB7_CLASSIFICATION"):
        config.profile_from_env()


def test_a_nonsense_port_is_not_swallowed(monkeypatch):
    monkeypatch.setenv("CIB7_DB_HOST", "postgres")
    monkeypatch.setenv("CIB7_DB_PORT", "five-thousand")
    with pytest.raises(ValueError, match="CIB7_DB_PORT"):
        config.profile_from_env()


def test_every_field_arrives(monkeypatch):
    for name, value in (
        ("CIB7_DB_HOST", "postgres"), ("CIB7_DB_PORT", "5433"), ("CIB7_DB_NAME", "engine"),
        ("CIB7_DB_SCHEMA", "camunda"), ("CIB7_DB_USER", "reader"),
        ("CIB7_DB_SSLMODE", "require"), ("CIB7_CLASSIFICATION", "prod"),
        ("CIB7_PROFILE_NAME", "environment-a"), ("CIB7_SOURCE_TZ", "Europe/Berlin"),
        ("CIB7_DISPLAY_TZ", "UTC"), ("CIB7_VALUES_ALLOWLIST", "/config/allowlist.csv"),
    ):
        monkeypatch.setenv(name, value)
    p = config.profile_from_env()
    assert (p.name, p.host, p.port, p.database, p.schema, p.user, p.sslmode) == (
        "environment-a", "postgres", 5433, "engine", "camunda", "reader", "require")
    assert (p.source_timezone, p.display_timezone) == ("Europe/Berlin", "UTC")
    assert p.values_allowlist_file == "/config/allowlist.csv"
    # An allowlist alone switches nothing on: without CIB7_VALUES_MODE the classification
    # decides, and here it is prod.
    assert p.values_mode_effective is False


def test_value_mode_from_the_environment(monkeypatch):
    monkeypatch.setenv("CIB7_DB_HOST", "postgres")
    monkeypatch.setenv("CIB7_CLASSIFICATION", "prod")
    monkeypatch.setenv("CIB7_VALUES_MODE", "true")
    # Explicitly on, but without an allowlist on prod: stays off.
    assert config.profile_from_env().values_mode_effective is False

    monkeypatch.setenv("CIB7_VALUES_ALLOWLIST", "/config/allowlist.csv")
    assert config.profile_from_env().values_mode_effective is True

    # Explicitly off beats even a test profile.
    monkeypatch.setenv("CIB7_CLASSIFICATION", "test")
    monkeypatch.setenv("CIB7_VALUES_MODE", "off")
    assert config.profile_from_env().values_mode_effective is False

    monkeypatch.setenv("CIB7_VALUES_MODE", "perhaps")
    with pytest.raises(ValueError, match="CIB7_VALUES_MODE"):
        config.profile_from_env()


def test_the_profile_carries_only_the_password_reference(monkeypatch):
    """No secret in the profile object -- not even when it comes from the environment."""
    monkeypatch.setenv("CIB7_DB_HOST", "postgres")
    monkeypatch.setenv("CIB7_DB_PASSWORD", "supersecret")
    p = config.profile_from_env()
    assert p.password_env == "CIB7_DB_PASSWORD"
    assert "supersecret" not in repr(p)
    assert p.resolve_password() == "supersecret"   # resolved only when connecting


def test_environment_and_profiles_file_coexist(monkeypatch, tmp_path):
    profiles_file = tmp_path / "profiles.yaml"
    profiles_file.write_text(
        "profiles:\n"
        "  - name: dump\n"
        "    kind: local_restore\n"
        "    classification: test\n"
        "    port: 55432\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIB7_PROFILES", str(profiles_file))
    monkeypatch.setenv("CIB7_DB_HOST", "postgres")
    monkeypatch.setenv("CIB7_PROFILE_NAME", "my-environment")

    profile = config.load_profiles()
    assert sorted(profile) == ["dump", "my-environment"]
    assert profile["dump"].kind is ProfileKind.LOCAL_RESTORE     # the dump route survives
    assert profile["my-environment"].host == "postgres"


def test_on_a_name_clash_the_environment_wins(monkeypatch, tmp_path):
    """The environment describes the place this process actually runs in."""
    profiles_file = tmp_path / "profiles.yaml"
    profiles_file.write_text(
        "profiles:\n"
        "  - name: my-environment\n"
        "    kind: direct\n"
        "    host: 127.0.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIB7_PROFILES", str(profiles_file))
    monkeypatch.setenv("CIB7_DB_HOST", "postgres")
    monkeypatch.setenv("CIB7_PROFILE_NAME", "my-environment")

    assert config.load_profiles()["my-environment"].host == "postgres"


def test_detection_queries_the_schema_from_the_profile():
    """A dump puts the engine in ``public``, other installations use a named schema -- both have
    to work.

    Checked statically, because detection does not run without a database: a hard-coded
    ``"public"`` in a query parameter would quietly return "no tables" against a schema-per-service
    installation -- which looks like an empty history, not like a configuration error.
    """
    from pathlib import Path

    from cib7explorer.db import detect

    text = Path(detect.__file__).read_text(encoding="utf-8")
    assert '"schema": "public"' not in text
    assert text.count('"schema": db.profile.schema') == 2


def test_the_connection_sets_the_search_path_from_the_profile():
    """Without this search path, none of the unqualified queries finds its tables."""
    from cib7explorer.db.connection import Database

    class FakeCursor:
        def __init__(self, log): self.log = log
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def execute(self, sql, params=None): self.log.append((sql, params))

    class FakeConn:
        def __init__(self): self.autocommit = False; self.log = []
        def cursor(self): return FakeCursor(self.log)

    profile = config.Profile(name="p", schema="camunda")
    conn = FakeConn()
    Database(profile)._configure(conn)

    settings = {params[0]: params[1] for sql, params in conn.log
               if "set_config" in sql and params and len(params) == 2}
    assert settings["search_path"] == "camunda"
    assert settings["default_transaction_read_only"] == "on"   # stays untouched
    assert conn.autocommit is True
