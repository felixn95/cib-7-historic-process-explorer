"""Connection profiles, paths and secrets.

Three rules hold throughout: no credentials in the repository, none in logs, none in error
messages. The profiles file lives outside the repository and stores only *references* to
secrets -- an environment variable or a file -- never the password itself.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .contracts import Classification, ProfileKind

# --- Paths -------------------------------------------------------------------------------

def config_dir() -> Path:
    return Path(os.environ.get("CIB7_CONFIG_DIR") or Path.home() / ".config" / "cib7-explorer")


def state_dir() -> Path:
    return Path(os.environ.get("CIB7_STATE_DIR") or Path.home() / ".local" / "state" / "cib7-explorer")


def profiles_path() -> Path:
    return Path(os.environ.get("CIB7_PROFILES") or config_dir() / "profiles.yaml")


def secrets_dir() -> Path:
    return state_dir() / "secrets"


def cache_dir() -> Path:
    return state_dir() / "cache"


def restores_dir() -> Path:
    return state_dir() / "restores"


def notes_path() -> Path:
    """Marks and their notes -- a file of its own so they survive a cache rebuild."""
    return Path(os.environ.get("CIB7_NOTES") or state_dir() / "marks.sqlite")


def base_path() -> str:
    """Path prefix the interface is served under (empty means directly below ``/``).

    Behind a proxy that mounts the interface in a sub-path (say ``/process-explorer/``),
    *every* generated link has to carry that prefix. Miss one and the first page still loads
    while every click on it goes nowhere -- a failure mode that never shows up in local
    development.

    The prefix is a property of the deployment, not of a single request: one installation is
    mounted at exactly one place. Hence an environment variable rather than a per-request
    ``root_path`` -- the value is then also available where no request object is at hand
    (Jinja global, ``case_url``, tests).

    Normalised to either an empty string or ``/something`` without a trailing slash, so that
    templates can write ``{{ base_path }}/profile/...`` without thinking about it.
    """
    raw = (os.environ.get("CIB7_BASE_PATH") or "").strip()
    if not raw or raw == "/":
        return ""
    return "/" + raw.strip("/")


def ensure_dirs() -> None:
    for d in (config_dir(), state_dir(), secrets_dir(), cache_dir(), restores_dir()):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_dir(), stat.S_IRWXU)


# --- Login (OIDC) ------------------------------------------------------------------------

@dataclass(frozen=True)
class OidcConfig:
    """Login against an OIDC provider such as Keycloak.

    Off as long as ``CIB7_OIDC_ISSUER`` is unset: on a development machine the tool starts
    without a login. On a shared server the opposite holds -- an interface onto production
    history with no login is not a basis for anything.
    """

    issuer: str
    client_id: str
    public_url: str                     # no trailing slash, prefix included
    session_secret: str
    client_secret: str | None = None    # empty => public client, code flow with PKCE
    scopes: str = "openid profile email"
    required_role: str | None = None    # without this role, nobody gets in
    session_max_age: int = 8 * 3600

    @property
    def is_public_client(self) -> bool:
        return not self.client_secret

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/redirect_uri"

    @property
    def metadata_url(self) -> str:
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"


def oidc_from_env() -> OidcConfig | None:
    """Read the login configuration from the environment; ``None`` means no login.

    Two values are demanded rather than guessed:

    * ``CIB7_PUBLIC_URL`` -- the externally visible address. It could be inferred from
      ``X-Forwarded-*`` headers, but the redirect URI has to match the one registered with the
      provider character for character. A guessed value turns into an error that only surfaces
      in the login dialog, where it is hard to read.
    * ``CIB7_SESSION_SECRET`` -- without a session secret every login is forgeable. Generating
      one at startup would be worse than demanding it: it changes on every restart and would
      silently tear up sessions as soon as more than one instance runs.

    A misconfiguration raises here instead of quietly falling back to "no login" -- otherwise
    the interface would stand open and nobody would notice.
    """
    issuer = (os.environ.get("CIB7_OIDC_ISSUER") or "").strip()
    if not issuer:
        return None

    def _required(name: str, purpose: str) -> str:
        value = (os.environ.get(name) or "").strip()
        if not value:
            raise ValueError(
                f"{name} is missing. It is mandatory once CIB7_OIDC_ISSUER is set ({purpose})."
            )
        return value

    client_id = _required("CIB7_OIDC_CLIENT_ID", "client registered with the provider")
    public_url = _required("CIB7_PUBLIC_URL", "redirect URI, must be registered with the provider")
    session_secret = _required("CIB7_SESSION_SECRET", "signature of the session cookie")
    if len(session_secret) < 32:
        raise ValueError(
            "CIB7_SESSION_SECRET is too short (32 characters minimum). "
            "Generate one with: openssl rand -base64 48"
        )

    max_age_raw = (os.environ.get("CIB7_SESSION_MAX_AGE") or "28800").strip()
    try:
        max_age = int(max_age_raw)
    except ValueError:
        raise ValueError(f"CIB7_SESSION_MAX_AGE='{max_age_raw}' is not a number of seconds.") from None

    return OidcConfig(
        issuer=issuer,
        client_id=client_id,
        public_url=public_url.rstrip("/"),
        session_secret=session_secret,
        client_secret=(os.environ.get("CIB7_OIDC_CLIENT_SECRET") or "").strip() or None,
        scopes=(os.environ.get("CIB7_OIDC_SCOPES") or "openid profile email").strip(),
        required_role=(os.environ.get("CIB7_OIDC_REQUIRED_ROLE") or "").strip() or None,
        session_max_age=max_age,
    )


# --- Defaults that encode conventions, not database facts ---------------------------------

#: Variable names treated as cross-process correlation keys by the case view's second track.
#:
#: Deliberately empty. There is no set of names that is right for more than one installation --
#: what identifies a business object is a modelling decision of whoever built the processes, and
#: a shipped guess would either miss the real keys or, worse, correlate on a name that happens to
#: mean something else here. The variable catalogue is the place to look them up; the names then
#: belong in the profile (``correlation_variables``) or in ``CIB7_CORRELATION_VARIABLES``.
#:
#: Until they are configured, the case view shows its first track (business key and parent chain)
#: and says plainly that the second one has nothing to work with.
DEFAULT_CORRELATION_VARIABLES: tuple[str, ...] = ()

#: Patterns for end events that mark a run as validation only. Naming for this is rarely
#: consistent within one installation, which is why these are patterns rather than fixed
#: names -- and why they are editable per profile: they describe a modelling convention, not
#: a property of the database.
DEFAULT_VALIDATION_ONLY_PATTERNS: tuple[str, ...] = (
    r"(?i)validation[_-]?only",
    r"(?i)validate[_-]?only",
    r"(?i)with[_-]?validation[_-]?only",
)

#: Patterns for end events that carry a validation *result* without meaning "validation only".
#: Kept separate so both can be counted apart instead of being lumped together.
DEFAULT_VALIDATION_RESULT_PATTERNS: tuple[str, ...] = (
    r"(?i)validation.*(failed|invalid|succeed|passed|done|valid)",
    r"(?i)(valid|invalid).*validat",
    r"(?i)validat",
)


@dataclass(frozen=True)
class Profile:
    """One way in. Above this layer nothing knows whether the connection leads to a restored
    dump or to a live database -- that is the point of the abstraction."""

    name: str
    kind: ProfileKind = ProfileKind.LOCAL_RESTORE
    classification: Classification = Classification.UNKNOWN

    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "camunda"
    #: Schema holding the engine tables. A restored dump usually puts them in ``public``;
    #: installations that follow a schema-per-service convention use a named schema instead.
    #: This belongs to the profile rather than to a role setting (``ALTER ROLE ... search_path``)
    #: because the interface should be able to *show* which schema it is reading -- a property
    #: of the connection has no business living inside somebody else's database.
    schema: str = "public"
    user: str = "explorer_ro"
    password_env: str | None = None
    password_file: str | None = None
    sslmode: str | None = None

    # kind=local_restore only
    dump_file: str | None = None
    container: str | None = None
    volume: str | None = None
    image: str | None = None
    admin_user: str = "postgres"

    # Time zones. Engine timestamps are stored without a zone, so the zone the writing JVM
    # ran in has to be recorded here -- it cannot be recovered from the data.
    source_timezone: str = "UTC"
    display_timezone: str = "Europe/Berlin"

    # Guard rails against a single page ruining somebody's afternoon.
    statement_timeout_ms: int = 30_000
    row_limit: int = 50_000
    pool_max_size: int = 4

    # Whether variable *values* may be shown; None => derived from the classification.
    values_mode: bool | None = None
    values_allowlist_file: str | None = None

    correlation_variables: tuple[str, ...] = DEFAULT_CORRELATION_VARIABLES
    validation_only_patterns: tuple[str, ...] = DEFAULT_VALIDATION_ONLY_PATTERNS
    validation_result_patterns: tuple[str, ...] = DEFAULT_VALIDATION_RESULT_PATTERNS

    ssh: dict[str, Any] = field(default_factory=dict)

    # -- derived --------------------------------------------------------------------------

    @property
    def values_mode_effective(self) -> bool:
        """Whether variable values may be rendered.

        The default follows the environment, not the program: only a profile classified as
        ``test`` shows values without an allowlist. Everywhere else the mode stays off until
        an allowlist names what is safe to show.
        """
        if self.values_mode is not None:
            if self.values_mode and self.classification is not Classification.TEST:
                return bool(self.values_allowlist_file)
            return self.values_mode
        return self.classification.values_mode_default

    @property
    def values_mode_locked_reason(self) -> str:
        if self.classification is Classification.TEST:
            return ""
        if not self.values_allowlist_file:
            return (
                f"Profile is classified '{self.classification.value}' and has no allowlist -- "
                "variable values stay hidden."
            )
        return ""

    @property
    def is_managed_container(self) -> bool:
        return self.kind is ProfileKind.LOCAL_RESTORE

    @property
    def container_name(self) -> str:
        return self.container or f"cib7-{self.name}"

    @property
    def volume_name(self) -> str:
        return self.volume or f"cib7-{self.name}-pgdata"

    def resolve_password(self, *, create_if_managed: bool = False) -> str | None:
        """In order: environment variable, file, secret managed by the tool itself."""
        if self.password_env:
            v = os.environ.get(self.password_env)
            if v:
                return v
        if self.password_file:
            p = Path(self.password_file).expanduser()
            if p.exists():
                return p.read_text(encoding="utf-8").strip()
        managed = secrets_dir() / f"{self.name}.pw"
        if managed.exists():
            return managed.read_text(encoding="utf-8").strip()
        if create_if_managed and self.is_managed_container:
            ensure_dirs()
            pw = secrets.token_urlsafe(24)
            managed.write_text(pw, encoding="utf-8")
            os.chmod(managed, stat.S_IRUSR | stat.S_IWUSR)
            return pw
        return None

    def admin_password(self, *, create_if_managed: bool = False) -> str | None:
        managed = secrets_dir() / f"{self.name}.admin.pw"
        if managed.exists():
            return managed.read_text(encoding="utf-8").strip()
        if create_if_managed and self.is_managed_container:
            ensure_dirs()
            pw = secrets.token_urlsafe(24)
            managed.write_text(pw, encoding="utf-8")
            os.chmod(managed, stat.S_IRUSR | stat.S_IWUSR)
            return pw
        return None


# --- Loading and creating -----------------------------------------------------------------

_ENUM_FIELDS = {"kind": ProfileKind, "classification": Classification}
_TUPLE_FIELDS = ("correlation_variables", "validation_only_patterns", "validation_result_patterns")


def _profile_from_dict(raw: dict[str, Any]) -> Profile:
    data = dict(raw)
    for key in ("password", "pw", "passwd"):
        if key in data:
            raise ValueError(
                f"Profile '{data.get('name')}': field '{key}' is not allowed. Passwords belong "
                "in an environment variable (password_env) or a file (password_file), not in "
                "the profiles file."
            )
    for key, enum in _ENUM_FIELDS.items():
        if key in data and data[key] is not None:
            data[key] = enum(str(data[key]))
    for key in _TUPLE_FIELDS:
        if key in data and data[key] is not None:
            data[key] = tuple(data[key])
    known = {f for f in Profile.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Profile '{data.get('name')}': unknown fields {sorted(unknown)}")
    return Profile(**data)


#: Name of the environment variable carrying the password of the environment profile. The
#: profile stores only this *reference* (``password_env``), never the value -- no secret in a
#: profile object, none in logs, none in error text.
ENV_PASSWORD_VAR = "CIB7_DB_PASSWORD"


def _name_list(env_var: str) -> tuple[str, ...]:
    """A comma-separated list of variable names from the environment.

    Empty is a legitimate answer and means "not configured", not "none found" -- the case view
    distinguishes the two.
    """
    raw = (os.environ.get(env_var) or "").strip()
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def profile_from_env() -> Profile | None:
    """A profile describing the environment the tool was started in.

    Why this exists: the same image should talk to a database next to it in a container
    network, to a restored dump on a laptop, or to a server database -- without mounting a
    profiles file from somebody's home directory. The ``docker-compose.yml`` of each
    environment states its own target.

    ``CIB7_DB_HOST`` decides whether this profile exists at all. Everything else has a
    default -- **except the classification**: without ``CIB7_CLASSIFICATION`` the profile
    counts as ``unknown``, and variable values stay hidden. That is the only defensible
    default when it is not established whose data sits at the other end; a profile that
    should show values has to say so out loud.
    """
    host = (os.environ.get("CIB7_DB_HOST") or "").strip()
    if not host:
        return None

    def _value(name: str, default: str) -> str:
        v = (os.environ.get(name) or "").strip()
        return v or default

    classification_raw = _value("CIB7_CLASSIFICATION", Classification.UNKNOWN.value)
    try:
        classification = Classification(classification_raw)
    except ValueError:
        allowed = ", ".join(c.value for c in Classification)
        raise ValueError(
            f"CIB7_CLASSIFICATION='{classification_raw}' is not a classification. "
            f"Allowed: {allowed}."
        ) from None

    port_raw = _value("CIB7_DB_PORT", "5432")
    try:
        port = int(port_raw)
    except ValueError:
        raise ValueError(f"CIB7_DB_PORT='{port_raw}' is not a port number.") from None

    # Unset means ``None``, that is "derive from the classification" -- the same as a missing
    # field in the profiles file. Explicitly switching values on for a target that is not
    # classified ``test`` only takes effect together with an allowlist; that rule lives in
    # ``Profile.values_mode_effective`` and is not repeated here.
    mode_raw = (os.environ.get("CIB7_VALUES_MODE") or "").strip().lower()
    if mode_raw in ("", "auto"):
        values_mode = None
    elif mode_raw in ("1", "true", "yes", "on"):
        values_mode = True
    elif mode_raw in ("0", "false", "no", "off"):
        values_mode = False
    else:
        raise ValueError(
            f"CIB7_VALUES_MODE='{mode_raw}' is unclear. Allowed: true, false or auto "
            "(unset = auto, which lets the classification decide)."
        )

    return Profile(
        name=_value("CIB7_PROFILE_NAME", "environment"),
        kind=ProfileKind.DIRECT,
        classification=classification,
        host=host,
        port=port,
        database=_value("CIB7_DB_NAME", "camunda"),
        schema=_value("CIB7_DB_SCHEMA", "public"),
        user=_value("CIB7_DB_USER", "explorer_ro"),
        password_env=ENV_PASSWORD_VAR,
        sslmode=(os.environ.get("CIB7_DB_SSLMODE") or "").strip() or None,
        source_timezone=_value("CIB7_SOURCE_TZ", "UTC"),
        display_timezone=_value("CIB7_DISPLAY_TZ", "Europe/Berlin"),
        values_mode=values_mode,
        values_allowlist_file=(os.environ.get("CIB7_VALUES_ALLOWLIST") or "").strip() or None,
        correlation_variables=_name_list("CIB7_CORRELATION_VARIABLES"),
    )


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    """Profiles from the profiles file plus, if configured, the one from the environment.

    Both routes stand side by side: on a workstation the file describes a restored dump, in a
    container the environment describes the target. On a name clash the environment wins,
    because it is the more specific statement -- it describes the place this process actually
    runs in.
    """
    out: dict[str, Profile] = {}
    p = path or profiles_path()
    if p.exists():
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for raw in doc.get("profiles") or []:
            prof = _profile_from_dict(raw)
            out[prof.name] = prof
    from_env = profile_from_env()
    if from_env is not None:
        out[from_env.name] = from_env
    return out


def get_profile(name: str, path: Path | None = None) -> Profile:
    profs = load_profiles(path)
    if name not in profs:
        raise KeyError(f"Profile '{name}' is not defined in {path or profiles_path()}")
    return profs[name]


def write_example_profiles(path: Path | None = None, *, dump_file: str | None = None) -> Path:
    """Create a profiles file with one dump profile and one commented live example."""
    p = path or profiles_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = f"""# Connection profiles for the process explorer.
# This file lives outside the repository. It holds NO passwords -- only references
# (password_env / password_file). For local_restore profiles the tool manages the
# password itself under {secrets_dir()}.

profiles:
  - name: demo-dump
    kind: local_restore
    classification: test          # test => variable values are shown by default
    dump_file: {dump_file or "/path/to/dump.backup"}
    container: cib7-explorer
    volume: cib7-pgdata
    port: 55432
    database: camunda
    user: explorer_ro
    # The zone the engine's JVM ran in. Engine timestamps carry no zone, so this cannot be
    # derived from the data -- state it, or every displayed time is off by the offset.
    source_timezone: UTC
    display_timezone: Europe/Berlin

  # - name: staging
  #   kind: direct
  #   classification: unknown     # unknown => variable values stay hidden
  #   host: db.internal
  #   port: 5432
  #   database: camunda
  #   schema: public
  #   user: explorer_ro
  #   password_env: CIB7_PW_STAGING
  #   source_timezone: UTC
  #   display_timezone: Europe/Berlin
"""
    p.write_text(doc, encoding="utf-8")
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return p


# --- Redaction ----------------------------------------------------------------------------

_URI_PW = re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)([^@\s]+)(@)")
_KV_PW = re.compile(r"(?i)\b(password)\s*=\s*('[^']*'|\"[^\"]*\"|\S+)")


def redact(text: str, *extra_secrets: str | None) -> str:
    """Strip secrets out of text before it reaches a log or an error message."""
    if not text:
        return text
    out = _URI_PW.sub(r"\1***\3", text)
    out = _KV_PW.sub(r"\1=***", out)
    for s in extra_secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "***")
    return out


def with_overrides(profile: Profile, **kw: Any) -> Profile:
    return replace(profile, **kw)
