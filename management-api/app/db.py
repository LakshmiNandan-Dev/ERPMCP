"""Two separate engines, deliberately never shared: identity_engine has
write access to identity_mappings/identity_mapping_org_scope; audit_engine
connects as a login role granted only ebsmcp_audit_reader, so a bug in this
API's audit code literally cannot write to the audit log — the database
rejects it, the same guarantee proven directly against Postgres when
audit-service's schema was built.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.config import load_settings

_settings = load_settings()

identity_engine: Engine = create_engine(_settings.identity_db_url)
audit_engine: Engine = create_engine(_settings.audit_db_url)
