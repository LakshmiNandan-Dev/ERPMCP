"""See identity-service/db/types.py for the rationale — this is the same
small type, copied rather than shared through a cross-package dependency
since it's ~15 lines and these two services are meant to become
independently deployable.
"""

from __future__ import annotations

import json

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import Text, TypeDecorator


class PortableJSON(TypeDecorator):
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return json.loads(value)
