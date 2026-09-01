"""Login against an OIDC provider such as Keycloak -- switchable off.

Why this lives inside the application rather than in front of it: on a development machine the
tool should start without a login, and on a shared server it must not be reachable without one.
Doing both from a single deployment is simplest when the application itself can, and the
environment decides (``CIB7_OIDC_ISSUER``).

A gatekeeper in front of it -- oauth2-proxy on a forwardAuth middleware -- is the other
defensible shape. It needs a container of its own and a confidential client, and it cannot pass
the authenticated user's role through to the application. That last point is the reason for this
choice: the role is meant to be able to influence who may see variable values.

What deliberately does NOT happen here:

* **No home-grown crypto.** Signature verification, JWKS retrieval and nonce checking are
  Authlib's job. Hand-rolled token validation is the classic place where a login only looks like
  it verifies something.
* **No token in the cookie.** The session cookie holds only who is signed in (name, roles,
  expiry) -- not the access or ID token. A stolen cookie therefore opens this interface, but no
  API of the surrounding platform.
* **No redirect to arbitrary targets.** The post-login destination is kept in the session and
  checked on return to be a path of this application.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from ..config import OidcConfig
from . import deps

log = logging.getLogger("cib7explorer.web.auth")

router = APIRouter()

#: Paths that have to stay reachable without a login.
#:  * ``/health`` -- otherwise no health check and no monitoring can form a judgement, and a
#:    login page returned with HTTP 200 would tell both of them "healthy", which is a lie.
#:  * ``/static/...`` -- the CSS and JavaScript of the login page itself.
#:  * the login routes.
PUBLIC_PATHS = ("/health", "/login", "/redirect_uri", "/logout")
PUBLIC_PREFIXES = ("/static/",)

SESSION_USER = "user"
SESSION_TARGET = "target_after_login"


def _oauth(cfg: OidcConfig):
    """The Authlib client, built once per configuration."""
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="keycloak",
        server_metadata_url=cfg.metadata_url,
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,      # None => public client
        client_kwargs={
            "scope": cfg.scopes,
            # For a public client, PKCE is the only thing protecting the authorization code --
            # and it does no harm to a confidential one. So: always.
            "code_challenge_method": "S256",
        },
    )
    return oauth


class Login:
    """Holds the configuration and the OAuth client; the whole login hangs off this."""

    def __init__(self, cfg: OidcConfig) -> None:
        self.cfg = cfg
        self.oauth = _oauth(cfg)

    @property
    def client(self):
        return self.oauth.keycloak


#: Set by ``install()`` when the environment asks for a login. Named ``provider`` rather than
#: ``login``, so that it cannot collide with the route function of that name.
provider: Login | None = None


# --- roles ----------------------------------------------------------------------------------

def roles_from_claims(claims: dict[str, Any], client_id: str) -> set[str]:
    """Roles from a Keycloak token: realm roles plus the ones of this client.

    Collected from both places because Keycloak keeps both and deployments differ -- anyone who
    attaches a role to the client will not find it under ``realm_access``.
    """
    roles: set[str] = set()
    realm = claims.get("realm_access") or {}
    roles.update(realm.get("roles") or [])
    resource = (claims.get("resource_access") or {}).get(client_id) or {}
    roles.update(resource.get("roles") or [])
    return {str(r) for r in roles}


def is_allowed(claims: dict[str, Any], cfg: OidcConfig) -> tuple[bool, str]:
    """Decide on access. The second return value is the reason for a refusal."""
    if not cfg.required_role:
        return True, ""
    roles = roles_from_claims(claims, cfg.client_id)
    if cfg.required_role in roles:
        return True, ""
    return False, (
        f"This interface requires the role '{cfg.required_role}'. "
        "You are signed in, but not authorised."
    )


# --- routes ---------------------------------------------------------------------------------

def _safe_target(candidate: str | None) -> str:
    """Only paths of this application -- never a foreign target taken from a parameter."""
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


@router.get("/login")
async def login(request: Request) -> Response:
    if provider is None:
        raise HTTPException(status_code=404, detail="No login configured.")
    request.session[SESSION_TARGET] = _safe_target(request.query_params.get("next"))
    return await provider.client.authorize_redirect(request, provider.cfg.redirect_uri)


@router.get("/redirect_uri")
async def finish_login(request: Request) -> Response:
    if provider is None:
        raise HTTPException(status_code=404, detail="No login configured.")
    cfg = provider.cfg
    try:
        token = await provider.client.authorize_access_token(request)
    except Exception as exc:  # noqa: BLE001 -- every failure ends as a readable page
        log.warning("login failed: %s", type(exc).__name__)
        return _error_page(
            "The login failed.",
            "The provider rejected the request, or the session expired in the meantime. "
            "Trying again usually helps.",
            status=401,
        )

    claims: dict[str, Any] = dict(token.get("userinfo") or {})
    # In Keycloak the roles live in the access token, not in userinfo.
    access_token = token.get("access_token")
    if access_token:
        claims.update(_claims_unverified(access_token))

    allowed, reason = is_allowed(claims, cfg)
    if not allowed:
        request.session.clear()
        return _error_page("No access", reason, status=403)

    request.session[SESSION_USER] = {
        "name": claims.get("preferred_username") or claims.get("name") or "unknown",
        "roles": sorted(roles_from_claims(claims, cfg.client_id)),
    }
    target = _safe_target(request.session.pop(SESSION_TARGET, "/"))
    return RedirectResponse(url=deps.with_base_path(target), status_code=303)


@router.get("/logout")
async def logout(request: Request) -> Response:
    """Sign out here, then continue to the provider's logout.

    Both steps are needed: signing out only here leaves the Keycloak session standing, and the
    next visit would silently be signed in again -- which looks as though logging out had not
    worked.
    """
    if provider is None:
        raise HTTPException(status_code=404, detail="No login configured.")
    request.session.clear()
    cfg = provider.cfg
    try:
        metadata = await provider.client.load_server_metadata()
        end_session = metadata.get("end_session_endpoint")
    except Exception:  # noqa: BLE001 -- an unreachable provider must not block logging out
        end_session = None
    if not end_session:
        return RedirectResponse(url=deps.with_base_path("/"), status_code=303)
    return RedirectResponse(
        url=f"{end_session}?client_id={quote(cfg.client_id)}"
            f"&post_logout_redirect_uri={quote(cfg.public_url + '/', safe='')}",
        status_code=303,
    )


def _claims_unverified(jwt_text: str) -> dict[str, Any]:
    """Read a JWT payload **without** verifying its signature -- for roles only.

    Acceptable here because the token was redeemed with the provider by Authlib moments earlier
    and the ID token was verified there; all that happens now is reading the role list out of the
    freshly obtained access token. For an access decision based on a token supplied by somebody
    else this would be wrong -- hence the name.
    """
    import base64
    import json

    try:
        payload = jwt_text.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001 -- a malformed token simply yields no roles
        return {}


def _error_page(title: str, text: str, *, status: int) -> HTMLResponse:
    """Deliberately without a template: this page has to work even when the session is broken,
    and it must not need anything from the database."""
    return HTMLResponse(
        status_code=status,
        content=(
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<title>{title} — Process Explorer</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:38rem;margin:4rem auto;"
            "padding:0 1rem;line-height:1.5}a{color:#0b5}</style></head><body>"
            f"<h1>{title}</h1><p>{text}</p>"
            f"<p><a href='{deps.with_base_path('/login')}'>Sign in again</a></p>"
            "</body></html>"
        ),
    )


# --- gatekeeper -----------------------------------------------------------------------------

def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def install(app: Any, cfg: OidcConfig) -> None:
    """Attach session handling, the gatekeeper and the routes to the application.

    Order matters: the gatekeeper is registered *before* the session middleware so that it runs
    *after* it when a request is handled -- Starlette processes the most recently added
    middleware first. The other way round, the gatekeeper would see no session and turn everyone
    away.
    """
    global provider
    provider = Login(cfg)

    @app.middleware("http")
    async def gatekeeper(request: Request, call_next: Callable[[Request], Awaitable[Response]]):
        # Compare without the prefix -- see deps.without_base_path.
        path = deps.without_base_path(request.url.path)
        if is_public_path(path) or request.session.get(SESSION_USER):
            return await call_next(request)

        # HTMX fragment requests must not get a login page written into a fragment; they receive
        # an instruction to reload the whole page instead.
        if request.headers.get("HX-Request") == "true":
            response = Response(status_code=401, content="Session expired.")
            response.headers["HX-Redirect"] = f"{cfg.public_url}/login"
            return response
        if request.method != "GET":
            return _error_page(
                "Not signed in",
                "This action requires a login. Please sign in again and retry.",
                status=401,
            )
        # On the way back the target is prefixed again by deps.with_base_path, so what goes in
        # here is the path WITHOUT the prefix -- otherwise it ends up in there twice.
        next_path = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(
            url=deps.with_base_path(f"/login?next={quote(next_path, safe='')}"), status_code=303)

    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.session_secret,
        session_cookie="cib7_session",
        max_age=cfg.session_max_age,
        same_site="lax",          # 'strict' broke the return trip from the provider
        https_only=urlparse(cfg.public_url).scheme == "https",
    )
    app.include_router(router)
    log.info(
        "login active: %s, client %s (%s)%s",
        cfg.issuer, cfg.client_id,
        "public with PKCE" if cfg.is_public_client else "confidential",
        f", role {cfg.required_role} required" if cfg.required_role else "",
    )
