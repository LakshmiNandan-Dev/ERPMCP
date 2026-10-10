"""Manufacturing toolset family — shop-floor execution (WIP), engineering
data (BOM/routing/ECO), quality, costing, maintenance (EAM) and period
close. Functional, Org-ID-scoped data, so every tool here resolves as
target_system="ebs", never "ebs_dba". Each category is its own module with
its own ToolSet export, same pattern as tools/scm; see tools/scoping.py for
the shared scoping helpers and tools/registry.py for resolve_scoped_call.

Not built yet (need a live instance to write reviewed SQL — see each
module's docstring): quality_results, nonconformance_summary,
pm_schedule_due, asset_failure_history, resource_capacity_load.
"""

from .costing import COSTING_TOOLSET
from .diagnose_job import DIAGNOSE_JOB_TOOLSET
from .eam import EAM_TOOLSET
from .engineering import ENGINEERING_TOOLSET
from .period_close import PERIOD_CLOSE_TOOLSET
from .quality import QUALITY_TOOLSET
from .wip import WIP_TOOLSET

__all__ = [
    "COSTING_TOOLSET",
    "DIAGNOSE_JOB_TOOLSET",
    "EAM_TOOLSET",
    "ENGINEERING_TOOLSET",
    "PERIOD_CLOSE_TOOLSET",
    "QUALITY_TOOLSET",
    "WIP_TOOLSET",
]
