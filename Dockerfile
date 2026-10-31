# base image for all stages
FROM python:3.15.1-alpine@sha256:54448f67d7c31c39e5cdd3ea2d6382395ce142806c284425f8e897d78a0465b3 AS base


# intermediary step with UV
FROM base AS uv
# copy uv bins
COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /bin/


# build stage
FROM uv AS builder
# install dependencies into venv
WORKDIR /app
COPY pyproject.toml uv.lock .
RUN uv sync --no-dev --locked --no-install-project


# tests stage
FROM uv AS tests
RUN apk add make libstdc++ shellcheck

# install all deps for testing
WORKDIR /tests
COPY pyproject.toml uv.lock .
RUN uv sync --all-extras --locked --no-install-project
COPY src/ src
COPY tests/ tests
COPY restic-pod/ restic-pod

# run tests
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
