from __future__ import annotations

from app.config import Settings, load_settings
from app.ebs.lookup import EBSLookupConnector, MockEBSLookupConnector, OracleEBSLookupConnector


def build_ebs_lookup_connector(settings: Settings) -> EBSLookupConnector:
    if settings.has_real_ebs_connection:
        return OracleEBSLookupConnector(
            dsn=settings.ebs_db_dsn,  # type: ignore[arg-type]
            user=settings.ebs_db_user,  # type: ignore[arg-type]
            password=settings.ebs_db_password,  # type: ignore[arg-type]
        )
    return MockEBSLookupConnector()


_settings = load_settings()
ebs_lookup_connector: EBSLookupConnector = build_ebs_lookup_connector(_settings)
