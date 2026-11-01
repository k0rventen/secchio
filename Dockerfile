# base image for all stages
FROM python:3.15.2-alpine@sha256:20a82dbc2783112ddb91d1534603f802d222a3e63e593cd002eb2ac534e70211 AS base


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

# Copy the venv from the builder
COPY --from=builder --chown=1000:1000 /app /app

# precompile the project's venv and the stdlib to .pyc (this results in a 20MB gain in compressed size, but a 4x speedup on startup)
RUN python -c "import compileall, sysconfig; compileall.compile_dir(sysconfig.get_paths()['stdlib']); compileall.compile_dir('/app/.venv/lib/python3.15/');"

# install code
WORKDIR /app
COPY src/ .

# user & env config
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
USER 1000

# start operator cluster-wide, exposing kopf's liveness endpoint for probes
CMD ["kopf", "run", "-A", "--liveness=http://0.0.0.0:8080/healthz", "handlers.py"]
