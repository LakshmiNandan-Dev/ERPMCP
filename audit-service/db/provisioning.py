"""One-time provisioning SQL for audit_log: adding the partitioned table's
primary key, creating its initial partitions, and setting up append-only
role grants.

Deliberately NOT wired as SQLAlchemy event.listen(..., "after_create", ...)
hooks on the Table in models.py. Those only fire under
metadata.create_all() — a dev/test convenience — and never fire when
Alembic applies a migration via op.create_table(), which is the actual
deployment path. Keeping this as plain SQL functions that the migration
calls via op.execute() means what gets tested and what actually deploys
are the same code path, not two that can silently diverge.

Postgres only — see models.py for why Oracle doesn't get partitioning here
(a separately licensed Enterprise Edition option) and gets a plain PK
instead.
"""

from __future__ import annotations

ADD_PRIMARY_KEY_POSTGRESQL = (
    "ALTER TABLE audit_log ADD CONSTRAINT pk_audit_log PRIMARY KEY (id, occurred_at)"
)
ADD_PRIMARY_KEY_ORACLE = "ALTER TABLE audit_log ADD CONSTRAINT pk_audit_log PRIMARY KEY (id)"

# Verified directly against a live instance: a row for a month with no
# partition yet is REJECTED outright, not silently accepted — for an audit
# table, a gap in partition provisioning must never mean audit writes start
# failing, so the DEFAULT partition exists specifically to catch that and
# keep writes succeeding while an operator catches up on provisioning the
# proper dated partition. Provisioning future months on an ongoing basis is
# an operational job (a scheduled task, or pg_partman), not something this
# migration can do once and be done with.
CREATE_INITIAL_PARTITIONS = """
DO $$
DECLARE
    month_start date := date_trunc('month', now());
BEGIN
    FOR i IN 0..2 LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS audit_log_%s PARTITION OF audit_log '
            'FOR VALUES FROM (%L) TO (%L)',
            to_char(month_start + (i || ' months')::interval, 'YYYY_MM'),
            month_start + (i || ' months')::interval,
            month_start + ((i + 1) || ' months')::interval
        );
    END LOOP;

    EXECUTE 'CREATE TABLE IF NOT EXISTS audit_log_default PARTITION OF audit_log DEFAULT';
END $$;
"""

# Append-only enforcement: two roles, neither ever granted UPDATE or
# DELETE. The absence of those grants is what makes this append-only, not
# a comment — verified directly by attempting an UPDATE as the writer role
# and confirming Postgres rejects it.
CREATE_APPEND_ONLY_ROLES_AND_GRANTS = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ebsmcp_audit_writer') THEN
        CREATE ROLE ebsmcp_audit_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ebsmcp_audit_reader') THEN
        CREATE ROLE ebsmcp_audit_reader NOLOGIN;
    END IF;
END $$;
REVOKE ALL ON audit_log FROM ebsmcp_audit_writer, ebsmcp_audit_reader;
GRANT INSERT, SELECT ON audit_log TO ebsmcp_audit_writer;
GRANT SELECT ON audit_log TO ebsmcp_audit_reader;
"""
