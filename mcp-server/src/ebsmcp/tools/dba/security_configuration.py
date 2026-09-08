"""Security configuration: 11 tools covering DB-level accounts, FND
application accounts/responsibilities, and segregation-of-duties
visibility. EBS stacks two separate security layers, and auditing only
one gives a false sense of completeness — DB-level accounts
(db_level_accounts) versus FND application accounts (everything else
here), secured through responsibilities rather than DB grants.

db_privilege_grants and db_links were added from the
oracle-base.com/dba/scripts Monitoring inventory (2026-09-02) — see
their own docstrings for how they differ from the existing
db_level_accounts/privileged_roles view. Columns verified against a
live instance before writing the SQL.

Several tools reuse the already-verified, already-in-production join
pattern from management-api/app/ebs/lookup.py (_RESPONSIBILITIES_SQL,
_SECURITY_PROFILE_SQL) — real, tested code in this repo, not just
inference — which is why the responsibility/profile-related tools below
run HIGH/MEDIUM-HIGH confidence rather than the more cautious ratings
elsewhere in this DBA effort.

RESPONSIBILITY_LEVEL_ID matches lookup.py's own constant exactly (not a
shared import — mcp-server and management-api don't share code, same
independence as identity/tables.py's own duplicated schema copy) —
lookup.py's own comment flags this as "worth cross-checking against
FND_PROFILE_OPTION_LEVELS on a real instance," a caveat that carries
over here unchanged.

effective_profile_value (composed — resolves Site->Application->
Responsibility->Server->Org->User precedence) is deliberately NOT built:
doing it correctly needs the full FND_PROFILE_OPTION_LEVELS mapping for
all six levels, and RESPONSIBILITY_LEVEL_ID is the only one with
confident footing. profile_values below covers the same underlying data
without synthesizing a precedence resolution I'm not sure of.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ebsmcp.tools.registry import ToolContext, ToolSet, resolve_scoped_call

# See lookup.py's own comment: worth cross-checking against
# FND_PROFILE_OPTION_LEVELS on a real instance rather than trusting blindly.
RESPONSIBILITY_LEVEL_ID = 10003

DbAccountsView = Literal["accounts", "privileged_roles"]

_DB_ACCOUNTS_QUERIES: dict[DbAccountsView, str] = {
    "accounts": (
        "SELECT username, account_status, profile, default_tablespace, password_change_date "
        "FROM DBA_USERS "
        "ORDER BY username"
    ),
    "privileged_roles": (
        "SELECT grantee, granted_role AS privilege, 'ROLE' AS privilege_type "
        "FROM DBA_ROLE_PRIVS WHERE granted_role = 'DBA' "
        "UNION ALL "
        "SELECT grantee, privilege, 'SYSTEM PRIVILEGE' AS privilege_type "
        "FROM DBA_SYS_PRIVS WHERE privilege LIKE '%ANY%' "
        "ORDER BY grantee"
    ),
}


def get_db_accounts_query(view: DbAccountsView) -> str:
    return _DB_ACCOUNTS_QUERIES[view]


DbPrivilegeGrantsView = Literal["system_privs", "object_privs"]


def build_db_privilege_grants_query(
    view: DbPrivilegeGrantsView, grantee: str | None
) -> tuple[str, dict[str, Any]]:
    """Distinct from db_level_accounts(view="privileged_roles") above,
    not a duplicate of it: that tool is a curated "who holds DBA-like
    power" view (DBA role + ANY-privileges only). This is the complete,
    unfiltered DBA_SYS_PRIVS/DBA_TAB_PRIVS grant inventory — for a full
    audit or looking up everything one specific grantee holds, not just
    the dangerous subset. Columns verified against a live instance
    (2026-09-02).

    Pure function, unit-tested directly — see
    test_security_configuration_queries.py.
    """
    binds: dict[str, Any] = {}
    where = ""
    if grantee:
        where = "WHERE grantee = :grantee "
        binds["grantee"] = grantee.upper()

    if view == "system_privs":
        sql = (
            "SELECT grantee, privilege, admin_option "
            "FROM DBA_SYS_PRIVS "
            f"{where}"
            "ORDER BY grantee, privilege "
            "FETCH FIRST 100 ROWS ONLY"
        )
    else:
        sql = (
            "SELECT grantee, owner, table_name, privilege, grantable "
            "FROM DBA_TAB_PRIVS "
            f"{where}"
            "ORDER BY grantee, owner, table_name "
            "FETCH FIRST 100 ROWS ONLY"
        )
    return sql, binds


DbLinksView = Literal["configured", "open"]

_DB_LINKS_QUERIES: dict[DbLinksView, str] = {
    "configured": (
        "SELECT owner, db_link, username, host, created "
        "FROM DBA_DB_LINKS "
        "ORDER BY owner, db_link"
    ),
    "open": (
        "SELECT inst_id, db_link, owner_id, logged_on, heterogeneous, open_cursors "
        "FROM GV$DBLINK "
        "ORDER BY inst_id, db_link"
    ),
}


def get_db_links_query(view: DbLinksView) -> str:
    """Columns verified against a live instance (2026-09-02).
    DBA_DB_LINKS never exposes the actual password (Oracle doesn't store
    it retrievably), only USERNAME — safe to surface as-is.

    Pure function, unit-tested directly — see
    test_security_configuration_queries.py.
    """
    return _DB_LINKS_QUERIES[view]


LoginSessionsView = Literal["active", "closed"]

_LOGIN_SESSIONS_QUERIES: dict[LoginSessionsView, str] = {
    "active": (
        "SELECT fu.user_name, fl.start_time, fl.pid "
        "FROM APPLSYS.FND_LOGINS fl "
        "JOIN APPLSYS.FND_USER fu ON fu.user_id = fl.user_id "
        "WHERE fl.end_time IS NULL "
        "ORDER BY fl.start_time DESC "
        "FETCH FIRST 50 ROWS ONLY"
    ),
    "closed": (
        "SELECT fu.user_name, fl.start_time, fl.end_time, fl.pid "
        "FROM APPLSYS.FND_LOGINS fl "
        "JOIN APPLSYS.FND_USER fu ON fu.user_id = fl.user_id "
        "WHERE fl.end_time IS NOT NULL "
        "ORDER BY fl.end_time DESC "
        "FETCH FIRST 50 ROWS ONLY"
    ),
}


def get_login_sessions_query(view: LoginSessionsView) -> str:
    """Pure function, unit-tested directly — see
    test_security_configuration_queries.py."""
    return _LOGIN_SESSIONS_QUERIES[view]


NamedUserStatus = Literal["active", "inactive"]

_NAMED_USER_COUNT_QUERIES: dict[NamedUserStatus, str] = {
    "active": (
        "SELECT COUNT(DISTINCT fu.user_id) AS named_user_count "
        "FROM APPLSYS.FND_USER fu "
        "WHERE fu.end_date IS NULL OR fu.end_date > SYSDATE"
    ),
    "inactive": (
        "SELECT COUNT(DISTINCT fu.user_id) AS named_user_count "
        "FROM APPLSYS.FND_USER fu "
        "WHERE fu.end_date IS NOT NULL AND fu.end_date <= SYSDATE"
    ),
}


def get_named_user_count_query(status: NamedUserStatus) -> str:
    """Same table and complementary WHERE clause as
    named_user_license_count's active view — inactive here means a
    populated, past end_date (a terminated/deactivated account), not a
    session-level concept like login_sessions(view="closed").

    Pure function, unit-tested directly — see
    test_security_configuration_queries.py.
    """
    return _NAMED_USER_COUNT_QUERIES[status]


def build_failed_login_query(username: str | None) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly — see
    test_security_configuration_queries.py.

    Columns verified against a live instance (2026-09-02): LOGIN_NAME and
    ATTEMPT_TIME are the real columns — the original guess (USER_NAME/
    START_DATE) was wrong (ORA-00904). LOGIN_NAME is the string actually
    typed at the login prompt, not a join to FND_USER — appropriate here,
    since a truly invalid username has no FND_USER row to join against.
    """
    where = ["attempt_time >= SYSDATE - 1"]
    binds: dict[str, Any] = {}
    if username:
        where.append("login_name = :username")
        binds["username"] = username.upper()
    sql = (
        "SELECT login_name, COUNT(*) AS failed_attempts, MAX(attempt_time) AS last_attempt "
        "FROM APPLSYS.FND_UNSUCCESSFUL_LOGINS "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY login_name "
        "ORDER BY failed_attempts DESC"
    )
    return sql, binds


def build_responsibility_assignments_query(
    username: str | None,
    responsibility_name: str | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly. Forward lookup (username),
    reverse lookup (responsibility_name), both, or neither (capped
    inventory scan) — same table/join lookup.py's own
    _RESPONSIBILITIES_SQL already proves.
    """
    where = []
    binds: dict[str, Any] = {}
    if username:
        where.append("fu.user_name = :username")
        binds["username"] = username.upper()
    if responsibility_name:
        where.append("frt.responsibility_name = :responsibility_name")
        binds["responsibility_name"] = responsibility_name
    where_clause = f"WHERE {' AND '.join(where)} " if where else ""

    sql = (
        "SELECT fu.user_name, fr.responsibility_id, fr.application_id, frt.responsibility_name, "
        "urg.start_date, urg.end_date "
        "FROM APPLSYS.FND_USER fu "
        "JOIN APPLSYS.FND_USER_RESP_GROUPS urg ON urg.user_id = fu.user_id "
        "JOIN APPLSYS.FND_RESPONSIBILITY fr "
        "  ON fr.responsibility_id = urg.responsibility_id AND fr.application_id = urg.responsibility_application_id "
        "JOIN APPLSYS.FND_RESPONSIBILITY_TL frt "
        "  ON frt.responsibility_id = fr.responsibility_id AND frt.application_id = fr.application_id "
        " AND frt.language = 'US' "
        f"{where_clause}"
        "ORDER BY fu.user_name, frt.responsibility_name "
        "FETCH FIRST 200 ROWS ONLY"
    )
    return sql, binds


def build_profile_values_query(
    profile_option_name: str,
    responsibility_id: int | None,
) -> tuple[str, dict[str, Any]]:
    """Pure function, unit-tested directly."""
    binds: dict[str, Any] = {"profile_option_name": profile_option_name}
    where = ["fpo.profile_option_name = :profile_option_name"]
    if responsibility_id is not None:
        where.append("fpov.level_id = :level_id AND fpov.level_value = :responsibility_id")
        binds["level_id"] = RESPONSIBILITY_LEVEL_ID
        binds["responsibility_id"] = responsibility_id

    sql = (
        "SELECT fpov.level_id, fpov.level_value, fpov.profile_option_value "
        "FROM APPLSYS.FND_PROFILE_OPTION_VALUES fpov "
        "JOIN APPLSYS.FND_PROFILE_OPTIONS fpo ON fpo.profile_option_id = fpov.profile_option_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY fpov.level_id, fpov.level_value"
    )
    return sql, binds


def _register(app: MCPServer, ctx: ToolContext) -> None:
    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def db_level_accounts(view: DbAccountsView = "accounts", instance: str | None = None) -> dict:
        """Report Oracle DB-level accounts — status/profile/tablespace
        (accounts; should be a short, known list — APPS, APPLSYS, a
        handful of DBA logins), or who holds the DBA role or other
        ANY-scoped system privileges (privileged_roles — the real
        "bypasses all application security" list). Defaults to accounts.
        Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured.
        """
        with resolve_scoped_call(
            ctx, tool_name="db_level_accounts", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_db_accounts_query(view))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "view": view,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def fnd_user_status(username: str, instance: str | None = None) -> dict:
        """Report one FND application account's active/inactive status
        and password expiry — flags an account that looks active but is
        past its end date, or the reverse. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="fnd_user_status", target_system="ebs_dba", params={"username": username},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT user_name, start_date, end_date, password_date, email_address, description "
                "FROM APPLSYS.FND_USER "
                "WHERE user_name = :username",
                {"username": username.upper()},
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "username": username,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def failed_login_attempts(username: str | None = None, instance: str | None = None) -> dict:
        """Report failed login attempts in the last 24 hours, grouped by
        user — potential brute-force activity, or confirms whether one
        specific user is actually locked out (pass username) and why.
        Omit username for a fleet-wide view. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx,
            tool_name="failed_login_attempts",
            target_system="ebs_dba",
            params={"username": username},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_failed_login_query(username)
            rows = connector.run(sql, binds)
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "username": username,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def responsibility_assignments(
        username: str | None = None,
        responsibility_name: str | None = None,
        instance: str | None = None,
    ) -> dict:
        """List responsibility assignments: pass username for a forward
        lookup (everything one user holds), responsibility_name for a
        reverse lookup (everyone holding one responsibility — e.g. the
        standard access-recertification query, or checking who holds an
        admin-tier responsibility like "System Administrator"), both to
        narrow further, or neither for a capped inventory scan. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx,
            tool_name="responsibility_assignments",
            target_system="ebs_dba",
            params={"username": username, "responsibility_name": responsibility_name},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_responsibility_assignments_query(username, responsibility_name)
            rows = connector.run(sql, binds)
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "username": username,
                "responsibility_name": responsibility_name,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def responsibility_privileges(responsibility_id: int, instance: str | None = None) -> dict:
        """Report a responsibility's function/menu exclusion rules
        (FND_RESP_FUNCTIONS) — the grant/exclude overrides layered on
        its menu, not the fully resolved "everything this responsibility
        can do" tree (that needs walking FND_MENU_ENTRIES recursively,
        not something this tool attempts). Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx,
            tool_name="responsibility_privileges",
            target_system="ebs_dba",
            params={"responsibility_id": responsibility_id},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            # Columns verified against a live instance (2026-09-02):
            # ACTION_TYPE doesn't exist on FND_RESP_FUNCTIONS (ORA-00904)
            # — dropped, not replaced, since there's no equivalent column.
            rows = connector.run(
                "SELECT responsibility_id, application_id, action_id, rule_type "
                "FROM APPLSYS.FND_RESP_FUNCTIONS "
                "WHERE responsibility_id = :responsibility_id "
                "ORDER BY action_id",
                {"responsibility_id": responsibility_id},
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "responsibility_id": responsibility_id,
                "rules": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def profile_values(profile_option_name: str, responsibility_id: int | None = None, instance: str | None = None) -> dict:
        """Report a profile option's configured value at every level it's
        set (e.g. MO_SECURITY_PROFILE_ID, MO_OPERATING_UNIT, or any other
        profile option name), optionally narrowed to one responsibility's
        own value. Does not resolve precedence to a single effective
        value — returns every level's raw value. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx,
            tool_name="profile_values",
            target_system="ebs_dba",
            params={"profile_option_name": profile_option_name, "responsibility_id": responsibility_id},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_profile_values_query(profile_option_name, responsibility_id)
            rows = connector.run(sql, binds)
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "profile_option_name": profile_option_name,
                "responsibility_id": responsibility_id,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def login_sessions(view: LoginSessionsView = "active", instance: str | None = None) -> dict:
        """List the 50 most recently started application logins with no
        recorded end_time (active — the standard check ahead of a
        maintenance window), or the 50 most recently closed-out logins
        (closed — session history / who was recently working). Same
        FND_LOGINS table, opposite end_time filter — not two separate
        tools, so an LLM never has to guess which one to reach for.
        Capped, not exhaustive: on an aged instance FND_LOGINS can carry
        millions of rows (crashed sessions, internal/concurrent-manager
        logins that don't always set end_time) — verified live
        (2026-09-02): an uncapped version of the active view took
        roughly a minute and returned ~20MB for a single real instance.
        FETCH FIRST both bounds the response and lets Oracle use a
        top-N sort instead of sorting the entire matching set. Defaults
        to active. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="login_sessions", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_login_sessions_query(view))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "view": view,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def named_user_license_count(status: NamedUserStatus = "active", instance: str | None = None) -> dict:
        """Report the count of FND_USER accounts that are currently
        active (status="active" — no end_date, or a future one; direct
        visibility into Named-User-Plus license exposure) or inactive
        (status="inactive" — a populated end_date in the past;
        terminated/deactivated accounts). Defaults to active. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx,
            tool_name="named_user_license_count",
            target_system="ebs_dba",
            params={"status": status},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_named_user_count_query(status))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "status": status,
                "results": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def sod_conflict_scan(responsibility_a: str, responsibility_b: str, instance: str | None = None) -> dict:
        """List every user holding both sides of a given responsibility
        pair — e.g. AP entry + AP payment, or PO requisition + PO
        approval. Takes the conflicting pair as parameters rather than a
        built-in ruleset: Oracle provides no universal SoD matrix, every
        customer defines their own based on their own responsibility
        names. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx,
            tool_name="sod_conflict_scan",
            target_system="ebs_dba",
            params={"responsibility_a": responsibility_a, "responsibility_b": responsibility_b},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(
                "SELECT DISTINCT fu.user_name "
                "FROM APPLSYS.FND_USER fu "
                "WHERE EXISTS ( "
                "  SELECT 1 FROM APPLSYS.FND_USER_RESP_GROUPS urg1 "
                "  JOIN APPLSYS.FND_RESPONSIBILITY_TL frt1 "
                "    ON frt1.responsibility_id = urg1.responsibility_id AND frt1.language = 'US' "
                "  WHERE urg1.user_id = fu.user_id AND frt1.responsibility_name = :responsibility_a "
                "    AND (urg1.end_date IS NULL OR urg1.end_date > SYSDATE) "
                ") AND EXISTS ( "
                "  SELECT 1 FROM APPLSYS.FND_USER_RESP_GROUPS urg2 "
                "  JOIN APPLSYS.FND_RESPONSIBILITY_TL frt2 "
                "    ON frt2.responsibility_id = urg2.responsibility_id AND frt2.language = 'US' "
                "  WHERE urg2.user_id = fu.user_id AND frt2.responsibility_name = :responsibility_b "
                "    AND (urg2.end_date IS NULL OR urg2.end_date > SYSDATE) "
                ") "
                "ORDER BY fu.user_name",
                {"responsibility_a": responsibility_a, "responsibility_b": responsibility_b},
            )
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "responsibility_a": responsibility_a,
                "responsibility_b": responsibility_b,
                "conflicting_users": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def db_privilege_grants(
        view: DbPrivilegeGrantsView = "system_privs",
        grantee: str | None = None,
        instance: str | None = None,
    ) -> dict:
        """Report the raw DB-level privilege grant inventory: direct
        system privileges (system_privs, e.g. SELECT ANY DICTIONARY), or
        object-level grants (object_privs, e.g. EXECUTE on a package).
        Pass grantee to see everything one user/role holds. Distinct
        from db_level_accounts(view="privileged_roles"), which is a
        curated "who has DBA-like power" view — this is the complete
        grant list. Capped at 100 rows; narrow with grantee on a large
        instance. Defaults to system_privs. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx,
            tool_name="db_privilege_grants",
            target_system="ebs_dba",
            params={"view": view, "grantee": grantee},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            sql, binds = build_db_privilege_grants_query(view, grantee)
            rows = connector.run(sql, binds)
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "view": view,
                "grantee": grantee,
                "grants": rows,
            }

    @app.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
        )
    )
    def db_links(view: DbLinksView = "configured", instance: str | None = None) -> dict:
        """Report every configured database link, owner and target
        username included (configured — a link with a broadly-privileged
        target account is a real audit finding), or every link with a
        currently open session (open). Defaults to configured. Call list_ebs_instances first if unsure which EBS instance names (e.g. PROD, UAT) this deployment has configured."""
        with resolve_scoped_call(
            ctx, tool_name="db_links", target_system="ebs_dba", params={"view": view},
            requested_instance=instance,
        ) as (identity, _effective_org_ids, connector):
            rows = connector.run(get_db_links_query(view))
            return {
                "environment": ctx.environment,
                "mapped_role": identity.mapped_role,
                "view": view,
                "results": rows,
            }


SECURITY_CONFIGURATION_TOOLSET = ToolSet(name="security_configuration", register=_register)
