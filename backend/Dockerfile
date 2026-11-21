# syntax=docker/dockerfile:1
# One backend image for migrate, api (and later request-engine / scheduler), built from the locked uv environment.
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg git gh \
 && install -d /usr/share/keyrings \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
# DuckLake needs these extensions; install them now so containers need no network on first use.
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL ducklake; INSTALL postgres')"
WORKDIR /app
CMD ["uvicorn", "hundred_acre.main:app", "--host", "0.0.0.0", "--port", "8000"]
