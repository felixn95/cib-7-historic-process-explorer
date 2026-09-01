# The explorer as a service -- meant to run next to the database it reads, in a container
# network, behind whatever proxy the surrounding stack already has.
#
# What this image deliberately cannot do:
#   * No Docker inside the container. Restoring a dump (`./explorer restore`) drives Docker and
#     stays work for the host; the container only reads. A read-only tool gets no write access
#     to the host daemon -- the restore view shows its explanatory box here instead.
#   * No profiles file baked in. It arrives as a mount (CIB7_PROFILES), so that no connection
#     details live in the image and changing the target needs no new image.
FROM python:3.13-slim

# psycopg comes as a binary wheel (see pyproject) -- no libpq-dev, no compiler needed.
RUN pip install --no-cache-dir uv==0.5.*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY cib7explorer ./cib7explorer
# With the oidc extra: the same image runs locally without a login and on a server with one
# (CIB7_OIDC_ISSUER decides). Building two images for that would be the kind of difference
# somebody forgets at the exact moment it matters.
RUN uv pip install --system --no-cache ".[oidc]"

# State lives outside the image: the cache is disposable, the mark list is not. Both therefore
# belong in a volume mounted at /state.
ENV CIB7_CONFIG_DIR=/config \
    CIB7_STATE_DIR=/state \
    CIB7_PROFILES=/config/profiles.yaml \
    PYTHONUNBUFFERED=1

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin explorer \
    && mkdir -p /config /state \
    && chown explorer:explorer /state
USER explorer

EXPOSE 8123

# --host 0.0.0.0: inside a container the network boundary is the container, not the interface.
# On a host, ./explorer still binds 127.0.0.1 -- that stays as it is.
# A path prefix (behind a proxy) is set via CIB7_BASE_PATH rather than hard-coded here, so the
# same image runs with and without one. Do NOT add --root-path: proxies that strip the prefix
# make it actively harmful (see docs/DESIGN-DECISIONS.md).
CMD ["python", "-m", "uvicorn", "cib7explorer.web.app:app", "--host", "0.0.0.0", "--port", "8123"]
