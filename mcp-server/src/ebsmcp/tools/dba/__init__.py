"""DBA persona toolset family — see identity-service/db/models.py for what
makes ebs_dba a distinct persona (all-or-nothing access, no Org ID scope).

Each DBA tool category from the architecture doc's catalog gets its own
module here with its own ToolSet export, same pattern as
instance_health.py. tools/registry.py holds the shared ToolContext/ToolSet/
resolve_scoped_call scaffolding every domain (dba, scm, hcm, fusion, ...)
builds on — nothing domain-specific belongs there.
"""

from .concurrent_processing import CONCURRENT_PROCESSING_TOOLSET
from .diagnose_request import DIAGNOSE_REQUEST_TOOLSET
from .high_availability_dr import HIGH_AVAILABILITY_DR_TOOLSET
from .index_health import INDEX_HEALTH_TOOLSET
from .instance_health import INSTANCE_HEALTH_TOOLSET
from .memory import MEMORY_TOOLSET
from .notification_diagnosis import NOTIFICATION_DIAGNOSIS_TOOLSET
from .output_printing import OUTPUT_PRINTING_TOOLSET
from .patch_version_tracking import PATCH_VERSION_TRACKING_TOOLSET
from .redo_archive_backup import REDO_ARCHIVE_BACKUP_TOOLSET
from .security_configuration import SECURITY_CONFIGURATION_TOOLSET
from .workflow import WORKFLOW_TOOLSET

__all__ = [
    "CONCURRENT_PROCESSING_TOOLSET",
    "DIAGNOSE_REQUEST_TOOLSET",
    "HIGH_AVAILABILITY_DR_TOOLSET",
    "INDEX_HEALTH_TOOLSET",
    "INSTANCE_HEALTH_TOOLSET",
    "MEMORY_TOOLSET",
    "NOTIFICATION_DIAGNOSIS_TOOLSET",
    "OUTPUT_PRINTING_TOOLSET",
    "PATCH_VERSION_TRACKING_TOOLSET",
    "REDO_ARCHIVE_BACKUP_TOOLSET",
    "SECURITY_CONFIGURATION_TOOLSET",
    "WORKFLOW_TOOLSET",
]
