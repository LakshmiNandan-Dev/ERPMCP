"""Finance toolset family — no toolsets registered yet.

This is where GL/AP/AR interface monitoring belongs, not tools/dba/:
those tables (GL_INTERFACE, AP_INVOICES_INTERFACE, RA_INTERFACE_LINES_ALL)
are functional business data, and a functional user's access is
Org-ID-scoped (target_system="ebs"), not the ebs_dba persona's
all-or-nothing grant — a DBA-persona tool exposing per-invoice/per-line
detail would bypass that scoping entirely. A prior tools/dba/interfaces.py
(gl_interface_backlog) got this wrong and was removed once the
categorization issue was caught, rather than widened further.

Each finance tool category gets its own module here with its own
ToolSet export, same pattern as tools/dba/instance_health.py; see
tools/registry.py for the shared ToolContext/ToolSet/resolve_scoped_call
scaffolding every domain builds on. Tools mounted here should call
resolve_scoped_call with target_system="ebs", not "ebs_dba".
"""

__all__: list[str] = []
