"""SCM (Supply Chain Management) toolset family — functional, Org-ID-scoped
data, so every tool here resolves as target_system="ebs", never "ebs_dba".
Each SCM tool category gets its own module with its own ToolSet export, same
pattern as tools/dba/instance_health.py; see tools/registry.py for the shared
ToolContext/ToolSet/resolve_scoped_call scaffolding every domain builds on.
"""

from .inventory import INVENTORY_TOOLSET
from .order_management import ORDER_MANAGEMENT_TOOLSET
from .purchasing import PURCHASING_TOOLSET
from .receiving import RECEIVING_TOOLSET
from .shipping import SHIPPING_TOOLSET

__all__ = [
    "INVENTORY_TOOLSET",
    "ORDER_MANAGEMENT_TOOLSET",
    "PURCHASING_TOOLSET",
    "RECEIVING_TOOLSET",
    "SHIPPING_TOOLSET",
]
