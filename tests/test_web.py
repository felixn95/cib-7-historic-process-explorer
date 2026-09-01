"""Tests for the connection and detection pages.

Principle: none of these tests needs a real database, except the single one marked
``@pytest.mark.integration``. Everywhere else the database layer (``connection.connect``) and the
detection module are replaced by monkeypatch.

Synthetic profiles get a random name component so that they never collide with a real profile or
with cache entries from earlier runs -- the cache is addressed per profile name, see
cib7explorer/web/app.py.
"""

from __future__ import annotations

import os

import sys
import types
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from cib7explorer import config as config_module
from cib7explorer.config import Profile
from cib7explorer.contracts import (
    Classification,
    DetectionResult,
    Feature,
    FeatureStatus,
    HistoryLevel,
    ProfileKind,
    RestorePhase,
    RestoreState,
    SchemaDeviation,
    TableInfo,
)
import cib7explorer.db as db_package
from cib7explorer.db import connection as connection_module
from cib7explorer.web.app import app

#: The profile the integration tests run against. Same source as the ``db`` fixture in
#: conftest.py -- a hard-coded name here would make the fixture and the URLs disagree.
REAL_PROFILE = os.environ.get("CIB7_TEST_PROFILE", "demo-dump")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def fake_restore_module(monkeypatch: pytest.MonkeyPatch):
    """Replace cib7explorer.restore.docker_restore with a harmless stub.

    Deliberately active even when the real module is present: these tests must never trigger an
    actual restore against a running container. A stub makes that certain regardless of what the
    real module happens to do.
    """

    fake_mod = types.ModuleType("cib7explorer.restore.docker_restore")

    class RestoreError(Exception):
        pass

    def _state(phase: RestorePhase, **kw) -> RestoreState:
        return RestoreState(
            profile_name="fake", dump_path="/nothing/here", dump_size_bytes=0,
            dump_fingerprint="fake", phase=phase, **kw,
        )

    fake_mod.RestoreError = RestoreError  # type: ignore[attr-defined]
    fake_mod.docker_available = lambda: (True, "Docker available (test stub)")  # type: ignore[attr-defined]
    fake_mod.status = lambda profile: _state(RestorePhase.ABSENT)  # type: ignore[attr-defined]
    fake_mod.read_state = lambda profile: _state(RestorePhase.ABSENT)  # type: ignore[attr-defined]
    fake_mod.start_background = lambda profile, force=False: _state(RestorePhase.CHECKING)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cib7explorer.restore.docker_restore", fake_mod)
    # "from ..restore import docker_restore" reads the already-set attribute of the parent
    # package before it even looks in sys.modules -- so if the real module was imported once in
    # this process, that attribute is already on the package and patching sys.modules alone would
    # no longer take effect.
    import cib7explorer.restore as restore_package
    monkeypatch.setattr(restore_package, "docker_restore", fake_mod, raising=False)
    return fake_mod


def _synthetic_profile(**overrides) -> Profile:
    name = overrides.pop("name", f"test-synth-{uuid.uuid4().hex[:8]}")
    defaults = dict(
        name=name,
        kind=ProfileKind.DIRECT,
        classification=Classification.TEST,
        host="127.0.0.1",
        port=5,
        database="x",
        user="x",
    )
    defaults.update(overrides)
    return Profile(**defaults)


class _FakeDatabase:
    """Replace db.connection.Database for tests that want no real connection."""

    def close(self) -> None:
        pass

    @property
    def read_only_proof(self):
        return types.SimpleNamespace(
            ok=True,
            privileges_clean=True,
            summary="read-only enforced, role holds no write privileges",
            probed_table="act_hi_procinst",
        )


# --- Routes: every one answers sensibly -------------------------------------------------------

def test_all_routes_respond(client: TestClient, fake_restore_module) -> None:
    assert client.get("/").status_code == 200
    assert client.get(f"/profile/{REAL_PROFILE}").status_code == 200
    assert client.get(f"/profile/{REAL_PROFILE}/detection").status_code == 200
    assert client.post(f"/profile/{REAL_PROFILE}/detection/rebuild").status_code == 200
    assert client.post(f"/profile/{REAL_PROFILE}/restore").status_code == 200
    assert client.get(f"/profile/{REAL_PROFILE}/restore/status").status_code == 200

    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert "profiles" in body
    assert "docker_available" in body


def test_an_unknown_profile_is_404_with_a_readable_message(client: TestClient) -> None:
    r = client.get("/profile/no-such-profile-xyz")
    assert r.status_code == 404
    assert "is not known" in r.json()["detail"]

    r2 = client.get("/profile/no-such-profile-xyz/detection")
    assert r2.status_code == 404


# --- The top bar ----------------------------------------------------------------------------

def test_the_header_shows_classification_and_read_only(client: TestClient) -> None:
    r = client.get(f"/profile/{REAL_PROFILE}")
    assert r.status_code == 200
    assert "badge-test" in r.text
    assert "read-only" in r.text


# --- A synthetic DetectionResult: deviations plus missing capabilities --------------------

def test_detection_shows_deviations_and_missing_capabilities(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _synthetic_profile()
    monkeypatch.setattr(config_module, "load_profiles", lambda path=None: {profile.name: profile})

    det = DetectionResult(
        profile_name=profile.name,
        classification=profile.classification,
        server_version="PostgreSQL 17.11 (Test)",
        database_name="camunda",
        connected_as="explorer_ro",
        session_is_read_only=True,
        installation_id="inst-test-1",
        engine_schema_version="7.24.0.0",
        history_level=HistoryLevel.AUDIT,
        history_level_raw="2",
        tables=[
            TableInfo(name="act_hi_procinst", exists=True, est_rows=100, total_bytes=8192, has_rows=True),
        ],
        features=[
            FeatureStatus(
                feature=Feature.PROCESS_INSTANCES, available=True, table="act_hi_procinst",
                table_exists=True, has_rows=True, est_rows=100,
            ),
            FeatureStatus(
                feature=Feature.VARIABLE_UPDATES, available=False, table="act_hi_detail",
                table_exists=True, has_rows=False, est_rows=0,
                reason=(
                    "act_hi_detail is empty -- variable updates are only written from "
                    "history level FULL onwards"
                ),
            ),
        ],
        deviations=[
            SchemaDeviation(table="act_ru_task", kind="missing_column", detail="column 'tenant_id_' is missing"),
        ],
        tenant_ids=[],
        detected_at=datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc),
        duration_ms=42,
    )

    monkeypatch.setattr(connection_module, "connect", lambda p: _FakeDatabase())

    fake_detect_mod = types.ModuleType("cib7explorer.db.detect")
    fake_detect_mod.detect = lambda db, profile: det  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cib7explorer.db.detect", fake_detect_mod)
    # See the comment in fake_restore_module: "from ..db import detect" can read the
    # already-set attribute of the parent package before it looks in sys.modules.
    monkeypatch.setattr(db_package, "detect", fake_detect_mod, raising=False)

    r = client.get(f"/profile/{profile.name}/detection")
    assert r.status_code == 200
    assert "schema deviation" in r.text
    assert "act_ru_task" in r.text
    assert "column &#39;tenant_id_&#39; is missing" in r.text
    assert "act_hi_detail is empty" in r.text


# --- prod profile: warning stripe and values OFF --------------------------------------------

def test_a_prod_profile_shows_the_warning_stripe_and_values_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _synthetic_profile(classification=Classification.PROD)
    monkeypatch.setattr(config_module, "load_profiles", lambda path=None: {profile.name: profile})

    r = client.get(f"/profile/{profile.name}")
    assert r.status_code == 200
    assert "PRODUCTION SYSTEM" in r.text
    assert "badge-prod" in r.text
    assert "Values OFF" in r.text
    assert profile.values_mode_locked_reason
    # Jinja escapes an apostrophe as &#39; -- compare against the HTML-escaped form.
    from markupsafe import escape

    assert str(escape(profile.values_mode_locked_reason)) in r.text


# --- no password in any response ------------------------------------------------------------

def test_no_password_in_responses(client: TestClient) -> None:
    profile = config_module.get_profile(REAL_PROFILE)
    pw = profile.resolve_password()
    assert pw, "expected a managed password for the test profile (secrets_dir())"

    for path in ("/", f"/profile/{REAL_PROFILE}"):
        body = client.get(path).text
        assert pw not in body


# --- Integration: against a real database ----------------------------------------------------------

@pytest.mark.integration
def test_detection_against_a_real_database(client: TestClient) -> None:
    r = client.get(f"/profile/{REAL_PROFILE}/detection")
    assert r.status_code == 200
    assert "AUDIT" in r.text
    assert "7.24.0" in r.text
