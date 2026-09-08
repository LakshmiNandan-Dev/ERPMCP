-- Runs once, only against a brand-new postgres data volume (standard
-- docker-entrypoint-initdb.d behavior — it's skipped on every later
-- restart). identity-service and audit-service own separate schemas
-- (separate Alembic histories, separate least-privilege roles in a real
-- deployment — see their .env.example comments), so they get separate
-- databases even though this compose stack points both at the same
-- Postgres server for local dev convenience.
CREATE DATABASE ebsmcp_identity;
CREATE DATABASE ebsmcp_audit;
