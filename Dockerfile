# base image
FROM python:3.14-alpine AS base

# build stage with uv
FROM base AS builder
# copy uv bins
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /bin/

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

# start operator cluster-wide
CMD ["kopf", "run", "-A", "handlers.py"]
