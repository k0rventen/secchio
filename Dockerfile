# base image
FROM python:3.15.0-alpine@sha256:2699b90f4d562a22e2dd076c82f964032f64814204bff36d271a54ba4cc2c7e2 AS base

# build stage with uv
FROM base AS builder
# copy uv bins
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /bin/

# install dependencies into venv
WORKDIR /app
COPY pyproject.toml uv.lock .
RUN uv sync --no-dev --locked --no-install-project


# tests stage
FROM base AS tests
RUN apk add make libstdc++ shellcheck
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/

WORKDIR /tests
COPY pyproject.toml uv.lock .
RUN uv sync --all-extras --locked --no-install-project
COPY src/ src
COPY tests/ tests
COPY restic-pod/ restic-pod

RUN make -f tests/Makefile verify


# runtime stage
FROM base

# unpriv user
RUN adduser app -SD -h /app -u 1000

# Copy the application from the builder
COPY --from=builder --chown=1000:1000 /app /app

# install code
WORKDIR /app
COPY src/ .

# user & env config
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
USER 1000

# start operator cluster-wide, exposing kopf's liveness endpoint for probes
CMD ["kopf", "run", "-A", "--liveness=http://0.0.0.0:8080/healthz", "handlers.py"]
