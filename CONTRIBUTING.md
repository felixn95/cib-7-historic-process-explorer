# Contributing

Bug reports, questions and patches are welcome. This file describes how to get a working
environment and which properties of the tool a change must not break.

## Environment

```bash
uv venv --python 3.13 .venv
uv pip install -e ".[dev,oidc]"
uv run pytest -q            # unit tests, no database needed (~16 s)
```

The unit suite needs neither a database nor Docker: an autouse fixture in `tests/conftest.py`
redirects the profiles file, the state directory and the mark list into a temporary directory. A
test that reads the machine's real configuration is a bug in the test, not a feature.

Tests against a real engine database are marked `integration` and deselected by default. Point
`CIB7_TEST_PROFILE` at a profile and run them explicitly:

```bash
CIB7_TEST_PROFILE=my-profile uv run pytest -m integration
```

## Invariants

These are the properties the tool exists for. A change that weakens one of them needs a very good
reason, and several of them are enforced by tests rather than by convention.

1. **Nothing writes to the engine database.** A select-only role, `default_transaction_read_only`,
   every query inside a transaction explicitly opened READ ONLY, and the static guard in
   `cib7explorer/db/sqlguard.py`. There is deliberately no code path that attempts a write --
   not even to prove read-only access, which is why the proof is assembled from session settings
   and table privileges instead.
2. **A missing number is never rendered as a zero.** Process history is only as complete as the
   engine's configured history level. Where a number cannot be determined, say so and give the
   reason.
3. **Every share carries its denominator.** `8,240 of 8,375 (98.4 %)`, never a bare percentage.
   Tests check this for the SQL that produces shares.
4. **The catalogue views read no variable values.** Enforced by a test that inspects every
   `_SQL_*` constant in `cib7explorer/db/varcatalog.py`, because "we did not intend to" is not a
   guarantee.
5. **Values are shown only where the profile allows it.** The decision lives in
   `cib7explorer/values.py` and nowhere else.
6. **Every query has a statement timeout and a row limit.** An exploration tool that can stall a
   shared database will not be used a second time.
7. **No credentials in logs, error messages or rendered pages.** `config.redact()` exists for
   this and is applied where errors reach the surface.

## Style

- Comments explain *why*, not *what*. The code says what it does.
- One decision, one place. If a rule appears in two modules, it belongs in neither.
- New behaviour comes with a test that fails without it.
- English throughout: identifiers, comments, commit messages, user-visible text.

## Reporting a problem

Please include the version (`/health` reports it), the engine's history level if relevant, and
what you expected instead. Do not paste variable values or business keys from a production
system into an issue.
