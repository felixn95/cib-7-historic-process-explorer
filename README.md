# Process Explorer for CIB seven / Camunda 7

[![tests](https://github.com/felixn95/cib-7-historic-process-explorer/actions/workflows/tests.yml/badge.svg)](https://github.com/felixn95/cib-7-historic-process-explorer/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![image](https://img.shields.io/badge/ghcr.io-container%20image-blue?logo=docker&logoColor=white)](https://github.com/felixn95/cib-7-historic-process-explorer/pkgs/container/cib-7-historic-process-explorer)

A tool for reading and understanding the process history of a CIB seven or Camunda 7 engine on
PostgreSQL. It **only ever reads**: browse the process definitions, follow one business object
from beginning to end, zoom into a single process instance.

**Read-only, provably.** A select-only role, `default_transaction_read_only`, every query inside a
transaction explicitly opened READ ONLY, plus a static guard that lets nothing but a single read
statement through. The interface shows the evidence -- gathered without ever attempting a write.

---

## Quick start

### With Docker, against a database you already have

Images are published to the GitHub Container Registry, for `linux/amd64` and `linux/arm64`:

```
ghcr.io/felixn95/cib-7-historic-process-explorer:0.1.0   # a released version
ghcr.io/felixn95/cib-7-historic-process-explorer:0.1     # the latest 0.1.x
ghcr.io/felixn95/cib-7-historic-process-explorer:latest  # the latest release
ghcr.io/felixn95/cib-7-historic-process-explorer:main    # the development head
```

Pin a version on a server. `latest` moves only when a release is tagged, `main` moves with every
merge -- convenient to try, wrong to depend on. The version tags come into being when a `v*.*.*`
tag is pushed, so until the first release only `main` and the commit tags exist.

```bash
docker run --rm -p 8123:8123 \
  -e CIB7_DB_HOST=your-postgres \
  -e CIB7_DB_NAME=camunda \
  -e CIB7_DB_SCHEMA=public \
  -e CIB7_DB_USER=explorer_ro \
  -e CIB7_DB_PASSWORD=… \
  -e CIB7_CLASSIFICATION=test \
  ghcr.io/felixn95/cib-7-historic-process-explorer:main
```

Then open **http://127.0.0.1:8123**. `CIB7_DB_HOST` is what decides that this profile exists at
all; everything else has a default. See [Configuration](#configuration).

The read-only role is worth creating properly:

```sql
CREATE ROLE explorer_ro LOGIN PASSWORD '…';
ALTER ROLE explorer_ro SET default_transaction_read_only = on;
ALTER ROLE explorer_ro SET statement_timeout = '30s';
GRANT CONNECT ON DATABASE camunda TO explorer_ro;
GRANT USAGE ON SCHEMA public TO explorer_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO explorer_ro;
-- so that tables created by a later engine upgrade stay readable:
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO explorer_ro;
```

### On a server, next to an existing stack

The image expects to sit in the same container network as the database and behind whatever proxy
the surrounding stack already has. Nothing about the target lives in the image: the connection
comes from the environment, so changing the target needs no new image.

```yaml
# docker-compose.yml
services:
  process-explorer:
    # Pin a released tag here (e.g. :0.1.0) as soon as one exists -- see the tag scheme above.
    image: ghcr.io/felixn95/cib-7-historic-process-explorer:main
    restart: unless-stopped
    environment:
      CIB7_DB_HOST: postgres            # the service name inside the network
      CIB7_DB_NAME: camunda
      CIB7_DB_USER: explorer_ro
      CIB7_DB_PASSWORD: ${EXPLORER_DB_PASSWORD:?set it in .env}
      CIB7_CLASSIFICATION: prod         # prod without an allowlist => no variable values
      CIB7_SOURCE_TZ: UTC               # the zone the engine's JVM wrote its timestamps in
      CIB7_DISPLAY_TZ: Europe/Berlin
      CIB7_BASE_PATH: /process-explorer # only when served under a path prefix
      # On a shared server, add the login. Without CIB7_OIDC_ISSUER there is none, and anybody
      # who reaches the port reads the process history -- see "Login" below. An incomplete OIDC
      # configuration fails at startup rather than degrading to an open interface.
      # CIB7_OIDC_ISSUER: https://keycloak/auth/realms/default
      # CIB7_OIDC_CLIENT_ID: process-explorer
      # CIB7_PUBLIC_URL: https://server/process-explorer
      # CIB7_SESSION_SECRET: ${EXPLORER_SESSION_SECRET:?openssl rand -base64 48}
    volumes:
      # The cache is disposable, the mark list is not. Both live here, so a container
      # replacement does not throw away what somebody wrote down.
      - explorer-state:/state
    networks: [engine]

volumes:
  explorer-state:

networks:
  engine:
    external: true
    name: the-name-of-your-existing-network
```

The package is public: `docker pull` needs **no credentials**, and no pull rate limit applies to
it. Nothing has to be configured on the server beyond reaching `ghcr.io`.

If the server cannot reach it, the image travels over SSH without any registry:

```bash
docker pull --platform linux/amd64 ghcr.io/felixn95/cib-7-historic-process-explorer:main
docker save ghcr.io/felixn95/cib-7-historic-process-explorer:main | ssh server 'docker load'
```

`--platform` matters when your workstation and the server disagree about their architecture: an
`arm64` image will not start on an `amd64` host, and the error it gives says `exec format error`
rather than anything about architectures.

### Locally, against a dump file

The tool can restore a `pg_dump -Fc` file into a container of its own and explore that -- useful
when the real database is not something you want to point a new tool at.

```bash
git clone https://github.com/felixn95/cib-7-historic-process-explorer.git
cd cib-7-historic-process-explorer
uv venv --python 3.13 .venv && uv pip install -e ".[dev]"
.venv/bin/python -m cib7explorer init-profiles --dump /path/to/dump.backup
./explorer restore        # loads the dump (a few minutes for a few GB), needs Docker
./explorer start          # prints the URL
```

### Commands

```bash
./explorer start      # bring up the database and the interface, print the URL
./explorer update     # after code changes: dependencies, tests, restart
./explorer restart    # restart the interface only (reloads the code)
./explorer stop       # stop the interface (the database keeps running)
./explorer status     # what is running, which data, which history level
./explorer test       # unit tests (~16 s); with --all also against a database (~3 min)
./explorer logs       # follow the interface log
./explorer psql       # psql on the restored database
./explorer restore    # (re)load the dump
./explorer network    # attach the restored database to a Docker network
```

Different port or profile: `CIB7_PORT=9000 ./explorer start`,
`CIB7_PROFILE=staging ./explorer status`.

`update` runs in that order for a reason: **if the tests are red, nothing is restarted** -- the
running interface stays on the last working state instead of being swapped for broken code. For
changes to templates or CSS, `./explorer restart` is enough; static files carry a content hash in
their URL, so a browser never shows stale CSS.

---

## The views

| View | What it answers |
|---|---|
| **Process definitions** | What is here at all: every definition with instance counts, root/subprocess role, runtime distribution, terminations, validation-only runs, business keys |
| **Variable catalogue** | Every process variable that ever occurred -- names, types, spread, scope, sizes, **without values**, so the list can be handed to anyone |
| **Cases** | Everything that happened to one business object, as a timeline including the gaps in between. Search, plus a browse list to find cases without knowing a key |
| **One instance** | Activities, user tasks, the BPMN model of the exact version that ran with the executed activities highlighted, and the variable values to open and copy |
| **Landscape** | Frequencies, call graph, co-occurrence, observed transitions, sequence variety, distributions -- every number clickable down to the cases behind it |
| **Marks** | Hold on to interesting cases and instances with your own notes, exportable, without variable values |

The definition and landscape views are **precomputed once** (roughly 90 and 10 seconds) and kept
with a visible timestamp; everything else answers straight from the database -- a case in under
100 ms.

---

## Configuration

Two routes, side by side. On a name clash the environment wins, because it describes the place the
process actually runs in.

### From the environment (containers)

| Variable | Default | Meaning |
|---|---|---|
| `CIB7_DB_HOST` | — | **Decides that the environment profile exists at all** |
| `CIB7_DB_PORT` | `5432` | |
| `CIB7_DB_NAME` | `camunda` | |
| `CIB7_DB_SCHEMA` | `public` | Schema holding the engine tables |
| `CIB7_DB_USER` | `explorer_ro` | |
| `CIB7_DB_PASSWORD` | — | The profile stores only the reference, never the value |
| `CIB7_DB_SSLMODE` | — | Passed to libpq |
| `CIB7_CLASSIFICATION` | `unknown` | `test` \| `unknown` \| `prod` -- decides whether values may be shown |
| `CIB7_VALUES_MODE` | `auto` | `true` \| `false` \| `auto` (the classification decides) |
| `CIB7_VALUES_ALLOWLIST` | — | Path to an allowlist of releasable variables |
| `CIB7_CORRELATION_VARIABLES` | — | Comma-separated variable names that identify a business object, for the case view's second track |
| `CIB7_PROFILE_NAME` | `environment` | Name shown in the interface |
| `CIB7_SOURCE_TZ` | `UTC` | The zone the engine's JVM wrote its timestamps in |
| `CIB7_DISPLAY_TZ` | `Europe/Berlin` | |
| `CIB7_CONNECT_TIMEOUT` | `10` | Seconds a connection attempt may take |
| `CIB7_BASE_PATH` | — | Path prefix when served behind a proxy, e.g. `/process-explorer` |
| `CIB7_PROFILES` | `<config>/profiles.yaml` | Profiles file |
| `CIB7_CONFIG_DIR`, `CIB7_STATE_DIR`, `CIB7_NOTES` | XDG defaults | Where configuration and state live |

### From a profiles file (workstations)

`~/.config/cib7-explorer/profiles.yaml`:

```yaml
profiles:
  - name: demo-dump
    kind: local_restore        # local_restore | direct | ssh_tunnel (not implemented yet)
    classification: test       # test => value mode on by default
    dump_file: /path/to/dump.backup
    container: cib7-explorer
    port: 55432
    database: camunda
    schema: public
    user: explorer_ro
    source_timezone: UTC       # the zone the engine wrote its timestamps in
    display_timezone: Europe/Berlin
    correlation_variables:     # optional, see below -- empty by default
      - orderNumber
      - customerNumber
```

**`correlation_variables`** deserves a word, because it is the one setting the tool cannot guess.
The case view has a second track: instances that carry the *same value of an identifying variable*
as this case, without belonging to it through the business key or the parent chain. Which variable
identifies a business object is a modelling decision of the installation, so the list ships empty
-- a guessed name would either miss the real keys or correlate on a name that means something else
here. The variable catalogue shows which names exist and how widespread they are; that is the
place to pick them. Until the list is configured, the view says so instead of quietly showing
nothing.

State and precomputations live under `~/.local/state/cib7-explorer/`: `cache/` (disposable),
`restores/` (restore progress), `secrets/`, `marks.sqlite` (**not** disposable).

### Behind a proxy

Set `CIB7_BASE_PATH` to the prefix the interface is mounted under. Do **not** additionally pass
uvicorn's `--root-path`: where the proxy strips the prefix, that combination breaks the static
mount and any path comparison in middleware. The reasoning is in
[docs/DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md).

### Login

Without `CIB7_OIDC_ISSUER` there is none -- right for a workstation. On a shared server that one
variable switches on a login against an OIDC provider such as Keycloak (authorization code with
PKCE, roles read from the token, `CIB7_OIDC_REQUIRED_ROLE` as an access condition). An incomplete
configuration deliberately fails at startup and does **not** degrade to an open interface.

| Variable | Meaning |
|---|---|
| `CIB7_OIDC_ISSUER` | e.g. `https://host/auth/realms/default` -- presence switches the login on |
| `CIB7_OIDC_CLIENT_ID` | Client registered with the provider |
| `CIB7_OIDC_CLIENT_SECRET` | Empty means a public client with PKCE |
| `CIB7_PUBLIC_URL` | Externally visible address including the prefix; the redirect URI is derived from it |
| `CIB7_SESSION_SECRET` | At least 32 characters, e.g. `openssl rand -base64 48` |
| `CIB7_OIDC_REQUIRED_ROLE` | Empty means every authenticated user gets in |
| `CIB7_SESSION_MAX_AGE` | Seconds, default 28800 |

---

## Security

This tool will eventually point at production databases. Therefore:

- **Read-only.** A role of its own without write privileges, `default_transaction_read_only`, every
  query inside a transaction explicitly opened READ ONLY, plus a static guard that lets only single
  read statements through. The evidence is gathered at connect time and displayed -- **without a
  test write**, from session flags and actual table privileges.
- **Statement timeout (30 s) and row limit (50 000 rows)** on every query. When a query hits a
  limit, that is reported rather than silently truncated. Row counts of large tables are estimates
  from `pg_class.reltuples`, never `count(*)`. Background builds get 300 s, because they run in a
  job with progress rather than inside a request.
- **No credentials in the repository.** The profiles file lives outside it and holds only
  references (`password_env`, `password_file`); a `password` field in it is rejected. For the dump
  route the tool manages the password itself under `~/.local/state/cib7-explorer/secrets/` with
  mode 600. Error messages pass through a redaction step.
- **The classification is visible.** Every profile is `test`, `unknown` or `prod`; the
  classification sits permanently in the page header and decides the value mode.

### Variable values

| Classification | Allowlist | Result |
|---|---|---|
| `test` | none | all values (configured deliberately) |
| `test` | present | only what it names |
| `unknown` / `prod` | none | **no values** |
| `unknown` / `prod` | present | only what it names |

The definition and catalogue pages **never** show values, not even when the value mode is on --
enforced by a test over every SQL statement in the catalogue module, not merely intended. Large
and binary values are never loaded automatically. An allowlist is naturally written from the
variable catalogue, whose CSV export has the right shape for it;
`cib7explorer.values.write_example_allowlist()` writes one.

---

## Where things live

The one architectural rule: **data access and interface are separate.** `cib7explorer.db` imports
nothing from `cib7explorer.web` and is callable and testable without a server, because the query
logic is the reusable part.

### Shared foundation

| File | Responsible for |
|---|---|
| `contracts.py` | Every data type. Knows neither database nor interface -- the contract between them |
| `config.py` | Connection profiles, paths, secrets, redaction of error text |
| `values.py` | Value mode and allowlist: one place decides whether a value may appear |
| `cache.py` | Local precomputations as SQLite -- the target database is never written to |
| `notes.py` | The mark list, a file of its own, never contains variable values |
| `jobs.py` | Background work with progress (catalogue and landscape builds) |
| `bpmn.py` | Read BPMN and draw it as SVG, without a third-party library |
| `__main__.py` | The command line: `serve`, `init-profiles`, `restore`, `detect` |

### Data access (`cib7explorer/db/`)

| File | Responsible for |
|---|---|
| `connection.py` | **The only way to the database.** Read-only enforced, statement timeout, row limit, redaction |
| `sqlguard.py` | Static screening: nothing but single read statements gets through |
| `detect.py` | What the connection is talking to: versions, history level, period, missing capabilities |
| `definitions.py` | The definition list: instance counts, roles, runtime distribution, validation runs |
| `varcatalog.py` | The variable catalogue -- **never reads values**, only names, types, lengths |
| `case.py` | Case closure, gaps, foreign keys, correlation |
| `instance.py` | Activities, user tasks and variables of one instance |
| `landscape.py` | Frequencies, call graph, transitions, sequences |

And `cib7explorer/restore/docker_restore.py`: restoring a dump into a container of the tool's own, with progress. The only component that writes anything -- to that container, never to an engine database.

### Interface (`cib7explorer/web/`)

| File | Routes |
|---|---|
| `app.py` | `/`, `/profile/{p}`, `/profile/{p}/detection`, `/health` |
| `views_definitions.py` | `/profile/{p}/definitions`, `…/variables`, CSV export |
| `views_cases.py` | `/profile/{p}/case`, `…/case/{key}` |
| `views_instance.py` | `/profile/{p}/instance/{id}`, `…/value.json`, `…/bpmn` |
| `views_landscape.py` | `/profile/{p}/landscape`, `…/landscape/cases` |
| `views_marks.py` | `/marks`, export as CSV and JSON |
| `deps.py` | Shared: load profiles, connection per request, formatting |
| `auth.py` | OIDC login, session, the gatekeeper in front of every route |

### Further reading

- [docs/DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md) — why it is built this way, with the
  measurements behind each decision
- [docs/DATA-MODEL.md](docs/DATA-MODEL.md) — what Camunda history holds and what it does not, per
  history level

---

## Tests

```bash
./explorer test          # unit tests, no database (~16 s)
./explorer test --all    # additionally the tests against a database (~3 min)
```

The integration tests need a reachable profile (`CIB7_TEST_PROFILE`, default `demo-dump`) and skip
themselves with a clear message when there is none. The expensive variable catalogue is built once
per run and shared.

Two kinds of test deserve a mention, because they cover failure classes that otherwise slip
through: `tests/test_pages.py` checks **every** page for error boxes and missing core content (a
page can answer with HTTP 200 and still be broken), and several tests check SQL statically -- that
the catalogue reads no values, that shares carry their denominators, that rare transitions are not
filtered away.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) -- it lists the invariants a change must not break, and how
to run the two test suites.

## License

Apache License 2.0, see [LICENSE](LICENSE).

CIB seven and Camunda are the trademarks of their respective owners. This is an independent tool
that reads their database schema; it is not affiliated with, endorsed by, or supported by either
project.
