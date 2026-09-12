"""Identity-mapping schema: Entra/AD identity -> EBS/Fusion role + Org ID
scope, per the architecture doc's onboarding flow.

Defined once as SQLAlchemy Core tables, not twice as hand-written parallel
Postgres/Oracle DDL files — the whole point of "pluggable Postgres or
Oracle" is that the two backends can't be allowed to drift apart, which
maintaining two independent SQL files invites over time. Dialect-specific
pieces (the JSON column type, the open-ended-mapping uniqueness index) are
tagged with .ddl_if(dialect=...) so each backend gets only the constructs
that apply to it — verified directly against a live Postgres instance
before writing this file for real; see the session notes.

Two tables:
  identity_mappings          — one row per (Entra subject, environment,
                                target_system, time period). A subject can
                                have several rows over time as their role
                                changes; effective_end_date closes one out.
  identity_mapping_org_scope — the Org ID / Business Unit list for one
                                mapping. One-to-many, not a delimited string
                                column, so a user needing several OUs isn't
                                encoded as a parsing problem later.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects import oracle

metadata = MetaData()

VALID_ENVIRONMENTS = ("dev", "test", "uat", "prod")
# ebs_dba is a distinct persona, not a third backend system: it's still
# EBS, but DBA-tool access (concurrent processing, tablespace, backup,
# ADOP, ...) isn't Org-scoped at all, and doesn't require the FND_USER
# account a functional mapping needs — see the CHECK constraint on
# target_username below. Fusion has no equivalent: it's a SaaS product,
# customers never get direct-DB DBA-style access to it.
VALID_TARGET_SYSTEMS = ("ebs", "fusion", "ebs_dba")
VALID_RESOLUTION_SOURCES = ("resolved_from_source", "manually_overridden")

# Generic DateTime(timezone=True) silently compiles to plain Oracle DATE —
# no time zone component at all — unless overridden per dialect like this.
# Verified directly (see session notes): without with_variant, Oracle would
# get a type that quietly drops timezone information on every timestamp in
# this table.
TZDateTime = DateTime(timezone=True).with_variant(oracle.TIMESTAMP(timezone=True), "oracle")

identity_mappings = Table(
    "identity_mappings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("entra_subject", String(320), nullable=False),
    Column("environment", String(10), nullable=False),
    Column("target_system", String(10), nullable=False),
    # The Entra identity and the backend account are two different
    # identifiers — an Entra UPN/email has no fixed relationship to an EBS
    # FND_USER.USER_NAME (or a Fusion username), so the mapping has to
    # record both explicitly rather than assuming one implies the other.
    # This is what onboarding actually maps: which EBS/Fusion account this
    # Entra identity is standing in for.
    #
    # Nullable, but not optional for functional mappings — the CHECK
    # constraint below requires it for everything except ebs_dba, since a
    # DBA persona's access isn't derived from an FND_USER account at all.
    Column("target_username", String(100), nullable=True),
    # For ebs/fusion: the functional pillar (finance, scm, manufacturing,
    # hcm, ...) this mapping belongs to — derived from the responsibility's
    # EBS Application (FND_APPLICATION) during onboarding, not typed by an
    # admin; see management-api's domain-lookup logic. Part of the
    # uniqueness index below specifically so one person can hold an open
    # Finance mapping and an open SCM mapping at the same time — grouping
    # by domain only means something if a person can actually belong to
    # more than one.
    #
    # NULL for ebs_dba — DBA access isn't domain-scoped at all, same
    # reasoning as target_username. A free VARCHAR rather than a DB-level
    # enum deliberately: EBS's own application list is larger than
    # "finance/scm/manufacturing/hcm" and likely to grow as more pillars
    # get onboarded, and a CHECK constraint would need a migration every
    # time. Validated against a known, extensible list in management-api
    # instead — same tradeoff already accepted for cross-service
    # duplication elsewhere in this project.
    Column("domain", String(40), nullable=True),
    # For ebs/fusion: the responsibility/role name, resolved from what's
    # actually assigned to target_username in the source system during
    # onboarding — see management-api's ebs_lookup module.
    # For ebs_dba: a descriptive label only (e.g. "Senior DBA — patching
    # access") — it doesn't drive access; a DBA mapping grants the full
    # DBA toolset, nothing narrower.
    Column("mapped_role", String(240), nullable=False),
    # The scope resolution flow from the architecture doc: resolved from
    # the responsibility's/role's own security setup by default; an admin
    # may narrow it (never widen — enforced by the entitlement filter at
    # query time, not here), which is what this column records.
    Column(
        "resolution_source",
        String(24),
        nullable=False,
        server_default="resolved_from_source",
    ),
    Column("effective_start_date", TZDateTime, nullable=False),
    # NULL = open-ended / still active. Closing a mapping out means setting
    # this, not deleting the row — history stays intact for audit.
    Column("effective_end_date", TZDateTime, nullable=True),
    # Which EBS database(s) this mapping may reach — a different axis from
    # domain/org scoping above, and one that DOES apply to ebs_dba (a DBA
    # needs routing between PROD/UAT/QA more than anyone; DBA access being
    # all-or-nothing within one database says nothing about which database).
    # false (the default, and every pre-existing row's value) means
    # "unrestricted — any instance configured in the deployment"; true means
    # identity_mapping_instance_scope's rows for this mapping are the
    # exhaustive allowlist, possibly zero rows for an explicit "no instance
    # access" grant. A plain empty-vs-present-rows convention (the way
    # identity_mapping_org_scope works) can't represent that distinction —
    # zero rows would be ambiguous between "unrestricted" and "deny all" —
    # hence this explicit flag instead.
    Column("instance_scope_restricted", Boolean, nullable=False, server_default=false()),
    Column("created_at", TZDateTime, nullable=False, server_default=func.now()),
    Column("created_by", String(320), nullable=False),
    Column("updated_at", TZDateTime, nullable=True, onupdate=func.now()),
    Column("updated_by", String(320), nullable=True),
    CheckConstraint(
        f"environment IN {VALID_ENVIRONMENTS}",
        name="ck_identity_mappings_environment",
    ),
    CheckConstraint(
        f"target_system IN {VALID_TARGET_SYSTEMS}",
        name="ck_identity_mappings_target_system",
    ),
    CheckConstraint(
        f"resolution_source IN {VALID_RESOLUTION_SOURCES}",
        name="ck_identity_mappings_resolution_source",
    ),
    CheckConstraint(
        "effective_end_date IS NULL OR effective_end_date > effective_start_date",
        name="ck_identity_mappings_date_order",
    ),
    # target_username is required for every functional mapping — an ebs or
    # fusion row with no backend account would be meaningless — but
    # optional for ebs_dba, whose access isn't derived from one. This is
    # the one invariant from the "DBA persona doesn't fit the functional
    # shape" gap worth enforcing at the database, not just in application
    # validation: a NULL target_username slipping into an ebs/fusion row
    # would silently break the entitlement flow that assumes it's there.
    CheckConstraint(
        "target_system = 'ebs_dba' OR target_username IS NOT NULL",
        name="ck_identity_mappings_username_required_unless_dba",
    ),
    # Same reasoning, same shape, for domain: required for every functional
    # mapping (a pillar-less ebs/fusion row can't be grouped for the
    # data-security purpose this column exists for), optional for ebs_dba.
    CheckConstraint(
        "target_system = 'ebs_dba' OR domain IS NOT NULL",
        name="ck_identity_mappings_domain_required_unless_dba",
    ),
    # The invariant this whole table exists to protect: at most one
    # currently-open mapping per (subject, environment, target_system,
    # domain). domain is part of the key — not just a descriptive column —
    # specifically so one person can hold an open Finance mapping and an
    # open SCM mapping in the same environment at once; without it, the
    # second onboarding would collide with the first. Two open mappings
    # for the same (subject, environment, target_system, domain) would
    # make "which Org IDs apply" ambiguous — exactly the kind of ambiguity
    # the entitlement filter is built to never have.
    #
    # domain is NULL for every ebs_dba row (DBA access isn't domain-scoped
    # at all), and both backends treat NULL specially in a unique index —
    # every NULL is distinct from every other NULL — which would silently
    # let the same person hold two simultaneous open ebs_dba mappings.
    # coalesce()/NVL() to a sentinel closes that hole; verified directly
    # against a live Postgres instance before writing this (a plain
    # multi-column unique index let a second NULL-domain row through, the
    # coalesce()'d version correctly rejected it — see session notes).
    #
    # Postgres gets a native partial unique index; Oracle gets the
    # function-based-index equivalent, since Oracle unique indexes
    # silently ignore any row with a NULL key component and so can't
    # express "unique among NULLs" any other way.
    Index(
        "ux_identity_mappings_open_ended",
        "entra_subject",
        "environment",
        "target_system",
        func.coalesce(text("domain"), text("''")),
        unique=True,
        postgresql_where=text("effective_end_date IS NULL"),
    ).ddl_if(dialect="postgresql"),
    Index(
        "ux_identity_mappings_open_ended",
        text(
            "(CASE WHEN effective_end_date IS NULL "
            "THEN entra_subject || ':' || environment || ':' || target_system "
            "|| ':' || NVL(domain, '__none__') END)"
        ),
        unique=True,
    ).ddl_if(dialect="oracle"),
)

identity_mapping_org_scope = Table(
    "identity_mapping_org_scope",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "identity_mapping_id",
        Integer,
        ForeignKey("identity_mappings.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # An Org ID (numeric-looking, EBS) or Business Unit identifier
    # (Fusion) — String because they don't share a format. Never present
    # for ebs_dba parent rows: DBA access is all-or-nothing, not
    # Org-scoped, so an ebs_dba mapping has zero rows in this table at
    # all — see identity_mappings' own comment and the CHECK constraint
    # rejecting any org_scope on an ebs_dba mapping in management-api's
    # schema layer.
    Column("org_id", String(64), nullable=False),
    Column("org_name", String(240), nullable=True),
    Column("resolved_from_source", Boolean, nullable=False, server_default=true()),
    UniqueConstraint("identity_mapping_id", "org_id", name="ux_mapping_org_scope_no_dupes"),
)

# The instance allowlist for one mapping — only meaningful when that
# mapping's instance_scope_restricted is true; see identity_mappings'
# column comment. Unlike identity_mapping_org_scope, not restricted to
# non-ebs_dba mappings — instance routing applies to every target_system.
identity_mapping_instance_scope = Table(
    "identity_mapping_instance_scope",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "identity_mapping_id",
        Integer,
        ForeignKey("identity_mappings.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("instance_name", String(64), nullable=False),
    UniqueConstraint(
        "identity_mapping_id", "instance_name", name="ux_mapping_instance_scope_no_dupes"
    ),
)

# One row per environment for now — this is deliberately a trust-anchor
# setting, not ordinary app data (it defines which Entra tenant and which
# tokens mcp-server accepts at all), so it's a separate, small table rather
# than folded into some general "settings" blob, and management-api treats
# writing to it as a distinct, more sensitive operation than editing an
# identity mapping. mcp-server reads this at startup only — a change here
# takes effect on the next restart, deliberately not hot-reloaded.
#
# Extending to genuinely multi-tenant (each licensed client with their own
# Entra tenant) means widening the unique constraint below to
# (environment, client_key) and adding that column — not a redesign, but
# not done now since this project hasn't committed to that yet.
entra_registrations = Table(
    "entra_registrations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("environment", String(10), nullable=False),
    Column("tenant_id", String(64), nullable=False),
    Column("audience", String(120), nullable=False),
    Column("subject_claim", String(60), nullable=False, server_default="preferred_username"),
    Column("resource_server_url", String(500), nullable=False),
    Column("created_at", TZDateTime, nullable=False, server_default=func.now()),
    Column("created_by", String(320), nullable=False),
    Column("updated_at", TZDateTime, nullable=True, onupdate=func.now()),
    Column("updated_by", String(320), nullable=True),
    CheckConstraint(
        f"environment IN {VALID_ENVIRONMENTS}",
        name="ck_entra_registrations_environment",
    ),
    UniqueConstraint("environment", name="ux_entra_registrations_one_per_environment"),
)

# Local admin accounts for admin-gui/management-api's own access control —
# deliberately not Entra/SSO-backed, unlike entra_registrations above
# (which is the opposite direction: the tenant mcp-server trusts for
# *end users*). Admin access to this tool is meant to stay independent of
# whichever customer Entra tenant a given deployment happens to serve —
# this is the product's own control plane, not something that should go
# dark if a customer's tenant does, and a licensable multi-tenant product
# can't assume every deployment even has one.
#
# password_hash is bcrypt, hashed by management-api (see
# app/auth/local.py) — this table only ever stores the hash, never a
# plaintext password, not even transiently in a migration.
admin_accounts = Table(
    "admin_accounts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("username", String(320), nullable=False),
    Column("password_hash", String(100), nullable=False),
    # Disabling access (an admin leaving, a compromised account) sets this
    # false rather than deleting the row — created_by/updated_by on every
    # identity_mappings row this account ever touched stay meaningful.
    Column("is_active", Boolean, nullable=False, server_default=true()),
    Column("created_at", TZDateTime, nullable=False, server_default=func.now()),
    # Nullable: the very first admin account has no admin who created
    # it — see management-api/scripts/create_admin.py.
    Column("created_by", String(320), nullable=True),
    Column("updated_at", TZDateTime, nullable=True, onupdate=func.now()),
    Column("updated_by", String(320), nullable=True),
    UniqueConstraint("username", name="ux_admin_accounts_username"),
)


# Per-deployment branding for the admin console. Lives here rather than in a
# VITE_ build arg because those are baked into the admin-gui image at build
# time: telling a customer to rebuild a container to change their own logo is
# not a branding feature. This product is licensed per client, so each
# deployment has to be able to wear its own name without a rebuild.
#
# Single row, enforced by the CHECK below rather than by convention — there is
# no per-environment dimension here (unlike entra_registrations): one
# deployment serves one customer, and a logo that changed between dev and prod
# would be a bug, not a feature.
#
# logo is a base64 data URI in a text column, not a file path or an external
# URL. A file upload would need a writable volume and a static route that
# neither service has, and a URL would break in exactly the air-gapped
# customer networks this product is built to run in.
branding = Table(
    "branding",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("company_name", String(120), nullable=True),
    Column("logo_data_uri", Text, nullable=True),
    Column("updated_at", TZDateTime, nullable=True, onupdate=func.now()),
    Column("updated_by", String(320), nullable=True),
    CheckConstraint("id = 1", name="ck_branding_single_row"),
)
