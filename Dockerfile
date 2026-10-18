# base image
FROM python:3.14.8-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72 AS base

# build stage with uv
FROM base AS builder
# copy uv bins
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /bin/

# install dependencies into venv
WORKDIR /app
COPY pyproject.toml uv.lock .
RUN uv sync --no-dev --locked --compile-bytecode --no-install-project


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
