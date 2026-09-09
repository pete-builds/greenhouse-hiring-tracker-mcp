FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

# Apply Debian security patches on top of the pinned base. Keeps the digest
# pin for reproducibility while picking up CVE fixes between base rebuilds.
#
# This used to be `ARG CACHE_BUST=<date>` with a note to bump the date by hand.
# The diagnosis in that note was exactly right and the mechanism never worked:
# no workflow ever passed --build-arg CACHE_BUST, so the arg sat at its default
# and the layer cached forever anyway. It had not been bumped since 2026-08-19,
# and on 2026-08-26 the Trivy gate was failing every PR in this repo on
# libssl3t64 CVE-2026-14456 while trixie-security had carried the fixed
# 3.5.7-1~deb13u2 for some time. A cache-bust that depends on a human
# remembering is a cache-bust that is stale exactly when it matters.
#
# trixie-security's Release file changes when and only when a security update
# is published, so keying the layer to it rebuilds precisely when there is
# something to install, with no date to maintain.
ADD https://deb.debian.org/debian-security/dists/trixie-security/Release /tmp/debian-security-release
RUN apt-get update \
    && apt-get -y upgrade \
    && rm -rf /tmp/debian-security-release /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install from the hash-pinned lockfile. --require-hashes refuses any package
# whose hash isn't in the file. Reproducible byte-for-byte.
# Regenerate with: uv pip compile requirements.txt -o requirements.lock \
#   --generate-hashes --python-version 3.13 --python-platform linux
# The platform flag matters: this lock is installed with --require-hashes in a
# Linux image, and resolving on macOS produces different transitive versions
# and wheel hashes.
COPY requirements.lock .
# pip is a build-time tool only: server.py and healthcheck.py never shell out to
# it. Purging it after the install removes pip's vendored third-party copies
# (pip/_vendor/msgpack, pip/_vendor/pkg_resources) which Trivy reports as
# msgpack and setuptools findings against the image even though nothing in the
# lockfile depends on them. Those copies are not separately upgradable, so
# deleting pip is the fix rather than a version bump.
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.14/site-packages/pip \
       /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
       /usr/local/lib/python3.14/ensurepip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

COPY clients/ ./clients/
COPY server.py .
COPY healthcheck.py .

# Non-root user with pinned UID for predictable bind-mount ownership.
RUN useradd --create-home --uid 1000 --shell /bin/bash mcp \
    && chown -R mcp:mcp /app
USER mcp

EXPOSE 3713

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python healthcheck.py || exit 1

CMD ["python", "server.py"]
