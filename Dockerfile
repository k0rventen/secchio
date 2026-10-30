# base image for all stages
FROM python:3.15.2-alpine@sha256:20a82dbc2783112ddb91d1534603f802d222a3e63e593cd002eb2ac534e70211 AS base


# intermediary step with UV
FROM base AS uv
# copy uv bins
COPY --from=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff /uv /bin/


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
