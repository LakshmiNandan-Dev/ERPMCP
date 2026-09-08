FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY db ./db
COPY migrations ./migrations

RUN pip install --no-cache-dir .

# One-shot migration runner, not a long-lived server — same shape as
# identity-service's Dockerfile. Applies alembic migrations (including the
# append-only role/partition provisioning in db/provisioning.py) against
# AUDIT_DB_URL once before management-api starts.
CMD ["alembic", "upgrade", "head"]
