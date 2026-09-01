"""Login against an OIDC provider.

What this file guards is not the happy path -- that shows up the first time anybody tries it.
It is the three cases nobody *sees*:

1. Without a login, no page may return content. A gatekeeper that protects the start page and
   lets a fragment through looks perfectly correct in a browser.
2. ``/health`` has to stay public. Otherwise the health check gets the login page with HTTP 200,
   the container counts as healthy, and the statement is false.
3. A broken configuration must not degrade to "no login". That is exactly when the interface
   would stand open and nobody would notice.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

ISSUER = "https://keycloak.example.com/realms/demo"
SECRET = "x" * 48

#: What the provider would serve at /.well-known/openid-configuration.
#:
#: Seeded rather than fetched: Authlib loads the metadata over HTTP on first use and remembers it
#: once `_loaded_at` is set. Without this seeding the suite would need a reachable provider -- and
#: it used to: while the issuer pointed at a real address, these tests silently ran against a
#: foreign server. A test that does not run without a network is not testing what it claims to.
PROVIDER_METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
    "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo",
    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
    "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
    "code_challenge_methods_supported": ["S256"],
    "_loaded_at": 1.0,
}

OIDC_ENV_VARS = (
    "CIB7_OIDC_ISSUER", "CIB7_OIDC_CLIENT_ID", "CIB7_OIDC_CLIENT_SECRET", "CIB7_PUBLIC_URL",
    "CIB7_SESSION_SECRET", "CIB7_OIDC_SCOPES", "CIB7_OIDC_REQUIRED_ROLE",
    "CIB7_SESSION_MAX_AGE",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in OIDC_ENV_VARS + ("CIB7_BASE_PATH", "CIB7_DB_HOST"):
        monkeypatch.delenv(name, raising=False)
    yield
    # This file reloads the application module in order to attach middleware. Without cleaning
    # up afterwards, an application WITH the gatekeeper would leak into other test files -- they
    # would then fail on a redirect, with the cause living in a different file entirely.
    for name in OIDC_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    import cib7explorer.web.app as app_module

    importlib.reload(app_module)


def _app_with_auth(monkeypatch, tmp_path, **extra_env):
    """Reload the application so that the login is installed at import time.

    The login hangs off middleware, and Starlette fixes middleware when the application is
    built -- attaching it afterwards does not work. Hence a deliberate re-import rather than a
    monkeypatch on the running object.
    """
    profiles_file = tmp_path / "profiles.yaml"
    profiles_file.write_text(
        "profiles:\n  - name: test-profile\n    kind: direct\n    host: nowhere.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIB7_PROFILES", str(profiles_file))
    monkeypatch.setenv("CIB7_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CIB7_NOTES", str(tmp_path / "marks.sqlite"))
    monkeypatch.setenv("CIB7_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("CIB7_OIDC_CLIENT_ID", "example-client")
    monkeypatch.setenv("CIB7_PUBLIC_URL", "https://explorer.example.com/process-explorer")
    monkeypatch.setenv("CIB7_SESSION_SECRET", SECRET)
    for k, v in extra_env.items():
        monkeypatch.setenv(k, v)

    import cib7explorer.web.app as app_module

    app_module = importlib.reload(app_module)

    from cib7explorer.web import auth as auth_module

    if auth_module.provider is not None:
        auth_module.provider.client.server_metadata.update(PROVIDER_METADATA)
    return app_module.app


# --- Configuration -----------------------------------------------------------------------

def test_no_issuer_means_no_login():
    from cib7explorer import config

    assert config.oidc_from_env() is None


def test_incomplete_configuration_fails_loudly(monkeypatch):
    from cib7explorer import config

    monkeypatch.setenv("CIB7_OIDC_ISSUER", ISSUER)
    with pytest.raises(ValueError, match="CIB7_OIDC_CLIENT_ID"):
        config.oidc_from_env()

    monkeypatch.setenv("CIB7_OIDC_CLIENT_ID", "example-client")
    with pytest.raises(ValueError, match="CIB7_PUBLIC_URL"):
        config.oidc_from_env()

    monkeypatch.setenv("CIB7_PUBLIC_URL", "https://host/process-explorer")
    with pytest.raises(ValueError, match="CIB7_SESSION_SECRET"):
        config.oidc_from_env()

    monkeypatch.setenv("CIB7_SESSION_SECRET", "too-short")
    with pytest.raises(ValueError, match="too short"):
        config.oidc_from_env()

    monkeypatch.setenv("CIB7_SESSION_SECRET", SECRET)
    cfg = config.oidc_from_env()
    assert cfg is not None
    assert cfg.is_public_client is True          # no secret: public client with PKCE
    assert cfg.redirect_uri == "https://host/process-explorer/redirect_uri"


def test_a_secret_makes_the_client_confidential(monkeypatch):
    from cib7explorer import config

    for name, value in (("CIB7_OIDC_ISSUER", ISSUER), ("CIB7_OIDC_CLIENT_ID", "process-explorer"),
                       ("CIB7_PUBLIC_URL", "https://host/px"),
                       ("CIB7_SESSION_SECRET", SECRET),
                       ("CIB7_OIDC_CLIENT_SECRET", "abc")):
        monkeypatch.setenv(name, value)
    assert config.oidc_from_env().is_public_client is False


def test_trailing_slash_in_the_public_url_is_tolerated(monkeypatch):
    from cib7explorer import config

    for name, value in (("CIB7_OIDC_ISSUER", ISSUER), ("CIB7_OIDC_CLIENT_ID", "c"),
                       ("CIB7_PUBLIC_URL", "https://host/px/"),
                       ("CIB7_SESSION_SECRET", SECRET)):
        monkeypatch.setenv(name, value)
    assert config.oidc_from_env().redirect_uri == "https://host/px/redirect_uri"


# --- gatekeeper ------------------------------------------------------------------------------

PAGES = ("/", "/marks", "/profile/test-profile", "/profile/test-profile/definitions",
          "/profile/test-profile/case", "/profile/test-profile/landscape",
          "/marks.csv", "/marks.json")


def test_without_a_session_no_page_gets_through(monkeypatch, tmp_path):
    app = _app_with_auth(monkeypatch, tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        for path in PAGES:
            response = client.get(path)
            assert response.status_code == 303, f"{path} was not protected"
            assert "/login" in response.headers["location"]
            # No content may travel along -- not even inside the redirect page.
            assert "Process definitions" not in response.text
            assert "test-profile" not in response.text


def test_health_stays_public(monkeypatch, tmp_path):
    """Otherwise the health check answers with a login page and the container counts as healthy."""
    app = _app_with_auth(monkeypatch, tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["profiles_file_present"] is True


def test_static_files_stay_public(monkeypatch, tmp_path):
    app = _app_with_auth(monkeypatch, tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/static/style.css").status_code == 200


def test_htmx_gets_a_redirect_instead_of_a_fragment(monkeypatch, tmp_path):
    """A fragment containing a login page would be written into the page as content."""
    app = _app_with_auth(monkeypatch, tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/profile/test-profile/detection", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"].endswith("/login")


def test_writing_requests_are_refused_not_redirected(monkeypatch, tmp_path):
    """Redirecting a POST to a login page loses the input silently."""
    app = _app_with_auth(monkeypatch, tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/marks/new", data={"kind": "business_key", "reference": "x"})
    assert response.status_code == 401


def test_login_redirects_to_the_provider(monkeypatch, tmp_path):
    """The only test that touches the provider -- via its metadata URL, without logging in."""
    app = _app_with_auth(monkeypatch, tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/login")
    if response.status_code >= 500:
        pytest.skip("provider unreachable (no network) -- the redirect cannot be checked.")
    assert response.status_code in (302, 303, 307)
    target = response.headers["location"]
    assert target.startswith(ISSUER + "/protocol/openid-connect/auth")
    assert "client_id=example-client" in target
    assert "code_challenge=" in target and "code_challenge_method=S256" in target
    assert "redirect_uri=https%3A%2F%2Fexplorer.example.com%2Fprocess-explorer%2Fredirect_uri" in target


def test_the_session_cookie_is_hardened(monkeypatch, tmp_path):
    app = _app_with_auth(monkeypatch, tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/login")
    cookie = response.headers.get("set-cookie", "")
    assert "cib7_session" in cookie
    assert "httponly" in cookie.lower()
    assert "secure" in cookie.lower()          # public_url is https
    assert "samesite=lax" in cookie.lower()


# --- Roles ------------------------------------------------------------------------------

def test_roles_come_from_realm_and_client():
    from cib7explorer.web import auth

    claims = {
        "realm_access": {"roles": ["offline_access", "user"]},
        "resource_access": {"example-client": {"roles": ["claims-handling"]}, "other-client": {"roles": ["foreign-role"]}},
    }
    roles = auth.roles_from_claims(claims, "example-client")
    assert roles == {"offline_access", "user", "claims-handling"}
    assert "foreign" not in roles              # roles of other clients do not count


def test_the_required_role_decides():
    from cib7explorer.config import OidcConfig
    from cib7explorer.web import auth

    base_path = dict(issuer=ISSUER, client_id="example-client", public_url="https://h/px",
                 session_secret=SECRET)
    without_role = OidcConfig(**base_path)
    with_role = OidcConfig(**base_path, required_role="process-explorer")

    claims_without_role = {"realm_access": {"roles": ["user"]}}
    claims_with_role = {"realm_access": {"roles": ["user", "process-explorer"]}}

    assert auth.is_allowed(claims_without_role, without_role)[0] is True     # no role required
    assert auth.is_allowed(claims_without_role, with_role)[0] is False
    allowed, reason = auth.is_allowed(claims_without_role, with_role)
    assert "process-explorer" in reason
    assert auth.is_allowed(claims_with_role, with_role)[0] is True


def test_the_redirect_target_stays_inside_the_application():
    """A target taken from a parameter must never lead outside."""
    from cib7explorer.web.auth import _safe_target

    assert _safe_target("/profile/x/case") == "/profile/x/case"
    assert _safe_target("https://evil.example.com/") == "/"
    assert _safe_target("//evil.example.com/") == "/"
    assert _safe_target(None) == "/"


# --- without a login, nothing changes -------------------------------------------------------

def test_without_a_login_everything_is_reachable(monkeypatch, tmp_path):
    profiles_file = tmp_path / "profiles.yaml"
    profiles_file.write_text(
        "profiles:\n  - name: test-profile\n    kind: direct\n    host: nowhere.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIB7_PROFILES", str(profiles_file))
    monkeypatch.setenv("CIB7_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CIB7_NOTES", str(tmp_path / "marks.sqlite"))

    import cib7explorer.web.app as app_module

    app_module = importlib.reload(app_module)
    with TestClient(app_module.app, follow_redirects=False) as client:
        assert client.get("/").status_code == 200
        assert client.get("/marks").status_code == 200
        assert client.get("/login").status_code == 404      # the route does not exist then


def test_redirects_carry_the_path_prefix(monkeypatch, tmp_path):
    """Behind a proxy, ``/process-explorer`` is stripped.

    A ``Location: /login`` would therefore send the browser to ``https://host/login`` -- an
    address nobody routes to this application. The bug only appears behind the proxy, never
    while developing.
    """
    app = _app_with_auth(monkeypatch, tmp_path, CIB7_BASE_PATH="/process-explorer")
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/profile/test-profile/case")
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/process-explorer/login?next="), location


def test_logout_goes_to_the_provider_and_clears_the_session(monkeypatch, tmp_path):
    app = _app_with_auth(monkeypatch, tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/logout")
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(ISSUER + "/protocol/openid-connect/logout")
    assert "post_logout_redirect_uri=https%3A%2F%2Fexplorer.example.com%2Fprocess-explorer%2F" in location


# --- prefix AND root_path together ----------------------------------------------------------
#
# The difference from the tests above is the client's `root_path`. That is how it used to run in a
# container: uvicorn started with `--root-path /process-explorer`, and Starlette then assembles
# `request.url.path` from root_path + path -- so the gatekeeper sees "/process-explorer/health"
# even though the proxy stripped the prefix. Without a root_path in the client the bug is
# invisible, which is exactly why it only surfaced on a server.
#
# For the same reason the service is NOT started with `--root-path` any more: Starlette then
# expects the path to still carry the prefix, and the static mount answers 404 under the stripped
# path -- an interface without CSS. The prefix comes from CIB7_BASE_PATH alone. The check here
# stays regardless: it keeps the gatekeeper correct even if a root_path is set somewhere.

def test_health_stays_public_even_with_a_root_path(monkeypatch, tmp_path):
    """Otherwise the container health check fails while the application is running fine."""
    app = _app_with_auth(monkeypatch, tmp_path, CIB7_BASE_PATH="/process-explorer")
    with TestClient(app, follow_redirects=False, root_path="/process-explorer") as client:
        response = client.get("/health")
    assert response.status_code == 200, response.headers.get("location")


def test_the_post_login_target_is_not_prefixed_twice(monkeypatch, tmp_path):
    """`next` is prefixed again on the way back -- so it has to go in WITHOUT the prefix.
    Otherwise a login lands on /process-explorer/process-explorer/..."""
    from urllib.parse import parse_qs, urlparse

    app = _app_with_auth(monkeypatch, tmp_path, CIB7_BASE_PATH="/process-explorer")
    with TestClient(app, follow_redirects=False, root_path="/process-explorer") as client:
        response = client.get("/profile/test-profile/case")
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/process-explorer/login?next="), location
    target = parse_qs(urlparse(location).query)["next"][0]
    assert target == "/profile/test-profile/case", target
