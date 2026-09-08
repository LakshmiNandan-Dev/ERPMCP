"""Fusion Applications toolset family — no toolsets registered yet.

Structurally different from dba/scm/hcm: those query EBS (on-prem Oracle)
via EBSConnector/resolve_scoped_call(target_system="ebs" or "ebs_dba").
Fusion is SaaS — real tools here will likely need a REST/SOAP-based
connector, not EBSConnector, even though identity/resolver.py's
TargetSystem already has "fusion" as a value and resolve_scoped_call
already accepts it. That connector doesn't exist yet; build it when the
first real Fusion tool needs it, not speculatively here.
"""

__all__: list[str] = []
