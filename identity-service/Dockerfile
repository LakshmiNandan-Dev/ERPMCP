FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY db ./db
COPY migrations ./migrations

RUN pip install --no-cache-dir .

# One-shot migration runner, not a long-lived server: management-api and
# mcp-server talk to Postgres directly and never import this package. This
# image's only job is `alembic upgrade head` against IDENTITY_DB_URL — see
# migrations/env.py — run once before management-api starts (see
# docker-compose.yml).
CMD ["alembic", "upgrade", "head"]
