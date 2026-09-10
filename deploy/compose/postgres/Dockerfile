# Layers the `pgvector` extension onto the first-party PostgreSQL image
# (docs/adr/0010 §2). This is not a second database server the way Khoj's
# `khoj-db` was (deploy/compose/khoj.docker-compose.yml) - pgvector is now a
# capability this project owns outright, added to an image this project
# already builds and pins by digest, so there is no external system's fixed
# base-image requirement to protect against.
#
# The base image already configures Postgres's own PGDG apt repository
# (apt.postgresql.org), so this installs the matching binary
# `postgresql-18-pgvector` package from that same, already-trusted
# repository rather than compiling from source - fewer moving parts, same
# trust boundary the base image already established.
#
# Base pinned by digest, per this repo's convention (see
# deploy/compose/docker-compose.yml's own header for the same pattern):
#   docker pull postgres:18.6-trixie
#   docker inspect --format='{{index .RepoDigests 0}}' postgres:18.6-trixie
FROM postgres:18.6-trixie@sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280

# Pinned to an exact package version, not the bare name: an unpinned
# `apt-get install` would silently pick up whatever PGDG happens to be
# serving on any given build day, breaking this repo's reproducible-image
# policy the same way an unpinned model slug or floating image tag would.
# Update by re-running, inside a container built from the base image above:
#   apt-get update && apt-cache policy postgresql-18-pgvector
#
# Unlike the digest-pinned base image above, this is an apt-repository pin,
# not a content-addressed one: PGDG's archive is not guaranteed to retain
# every historical build forever, so a `--no-cache` rebuild long after this
# was written could fail with "version not found" even though nothing in
# this repository changed. If that happens, re-run the `apt-cache policy`
# command above against the current base image to find a replacement pin -
# there is no other remedy for an apt-repository pin going stale upstream.
#
# .github/workflows/ci.yml's "Install pgvector into the CI Postgres service"
# step reads this same value out of this file rather than duplicating it, so
# updating it here is the only place that needs to change.
ARG PGVECTOR_PACKAGE_VERSION=0.8.6-1.pgdg13+1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         "postgresql-18-pgvector=${PGVECTOR_PACKAGE_VERSION}" \
    && rm -rf /var/lib/apt/lists/*
