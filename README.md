# EBSMCP

An MCP server that puts Oracle E-Business Suite in reach of an AI assistant —
without handing that assistant a database password or a blanket view of the
data.

Every tool call is resolved to a real person's Entra ID identity, narrowed to
what that person is actually entitled to see in EBS, routed to a specific EBS
instance, and written to an audit trail. The model never chooses its own
scope; it can only ask questions inside a boundary the platform sets.

> **License:** proprietary. Evaluation and testing use is permitted; production
> and commercial use require a separate agreement. See [LICENSE](LICENSE).

---

## Contents

- [What's in the box](#whats-in-the-box)
- [How a tool call is secured](#how-a-tool-call-is-secured)
- [Tool catalog](#tool-catalog)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [User onboarding](#user-onboarding)
- [Security model](#security-model)
- [Auditing](#auditing)
- [Development](#development)

---

## What's in the box

Five services. The MCP server is the product; the rest exist so that a human
administrator, not a config file, decides who can see what.

| Service | Stack | Port | Role |
|---|---|---|---|
| [mcp-server/](mcp-server/) | Python, MCP SDK | 8080 | The MCP endpoint. 66 read-only EBS tools across 13 toolsets. |
| [management-api/](management-api/) | FastAPI | 8010 | Admin API — onboarding, Entra config, audit queries. |
| [admin-gui/](admin-gui/) | React + Vite | 5174 | Admin console for the above. |
| [identity-service/](identity-service/) | SQLAlchemy + Alembic | — | Schema/migrations for the identity-mapping store. |
| [audit-service/](audit-service/) | SQLAlchemy + Alembic | — | Schema/migrations for the append-only audit log. |

The two `*-service` directories are schema owners with their own Alembic
histories — they run as one-shot migration containers, not long-lived
processes.

---

## How a tool call is secured

Five steps, in this order, for every single call. The shared implementation is
`resolve_scoped_call` in [mcp-server/src/ebsmcp/tools/registry.py](mcp-server/src/ebsmcp/tools/registry.py) — no
individual tool re-implements any of it.

```
   MCP client (Copilot, Claude Desktop, inspector)
     │
     │  bearer token
     ▼
 1. AUTHENTICATE     EntraTokenVerifier — validate signature against the
                     tenant JWKS, check aud / iss / exp. The subject claim
                     becomes the caller's identity.
     │
     ▼
 2. RESOLVE          Look up (subject, target_system) in identity_mappings.
                     No open mapping → LookupError → denied. Never treated
                     as "unrestricted".
     │
     ▼
 3. ENTITLE          EntitlementFilter intersects the identity's allowed
                     Org IDs with whatever the call asked for. Call
                     parameters may NARROW scope; they can never widen it.
     │
     ▼
 4. ROUTE            Pick the EBS instance. Auto-selected when the caller
                     is entitled to exactly one; otherwise `instance` must
                     be passed and is checked against their allowlist.
     │
     ▼
 5. AUDIT            One AuditRecord per call — on success, denial, AND
                     error. Written before the result is returned.
     │
     ▼
   Oracle EBS (read-only account, SELECT only)
```

The design assumption behind step 3 is worth stating plainly, because it is
what makes the rest safe:

> **An LLM-constructed tool call is not a trusted source of authorization.**
> Only the resolved identity is. This is enforced in one place —
> [policy/entitlement.py](mcp-server/src/ebsmcp/policy/entitlement.py) — rather than trusted to 66 individual
> tools.

---

## Tool catalog

66 tools, all read-only (`read_only_hint=True`, `destructive_hint=False`),
grouped into 13 mountable toolsets.

Toolsets exist for a specific reason: 66 tools presented as one flat list is a
selection-accuracy problem for the model, not a cosmetic one. A deployment
mounts only the toolsets it needs.

| Toolset | Tools | What it covers |
|---|---|---|
| `instance_health` | 24 | Tablespaces, sessions, locks, waits, undo/temp, objects, stats, alert log |
| `security_configuration` | 11 | FND users, responsibilities, grants, SoD scan, failed logins, DB links |
| `memory` | 5 | SGA, PGA, cache efficiency, work areas, advisors |
| `concurrent_processing` | 4 | Requests, manager status/capacity, load trend |
| `patch_version_tracking` | 4 | ADOP sessions, patch history, editions, component registry |
| `redo_archive_backup` | 4 | Redo logs, archive status, backup state, archive pipeline |
| `workflow` | 4 | Workflow activities, backlog, notifications, mailer status |
| `output_printing` | 3 | OPP status, output file errors, printer registration |
| `high_availability_dr` | 2 | HA/DR status, database services |
| `index_health` | 2 | Unusable indexes, non-indexed foreign keys |
| `diagnose_request` | 1 | Guided diagnosis of one concurrent request |
| `notification_diagnosis` | 1 | Guided diagnosis of a stuck notification |
| `health` | 1 | `server_health` — pipeline smoke test |

Start with **`list_ebs_instances`** — it reports which instances the calling
identity may actually reach, which is what every other tool's optional
`instance` parameter expects.

---

## Quick start

The whole stack runs against Postgres with a mocked EBS connector, so you can
see it work before any Oracle credentials exist.

```bash
docker compose up --build
```

| | |
|---|---|
| Admin console | http://localhost:5174 |
| Management API docs | http://localhost:8010/docs |
| MCP endpoint | http://localhost:8080 |

With `EBS_DB_DSN` unset, the MCP server uses `MockEBSConnector` and identity
resolution falls back to a single stub mapping — enough to exercise the full
pipeline (resolve → entitle → route → audit) end to end.

### Running the MCP server alone

```bash
cd mcp-server
pip install -e ".[dev]"
python -m ebsmcp.server          # stdio transport
```

stdio is the mode to use with the MCP inspector or Claude Desktop. It has no
bearer token, so identity comes from `EBSMCP_DEV_IDENTITY_SUBJECT`.

---

## Configuration

Everything is environment variables, read once at process start. Copy
[mcp-server/.env.example](mcp-server/.env.example) to get started.

### Choosing a transport

| | `stdio` | `streamable-http` |
|---|---|---|
| Use for | Local dev, one client | Hosted deployment |
| Auth | None — dev subject | Entra bearer tokens |
| Set | `EBSMCP_TRANSPORT=stdio` | `EBSMCP_TRANSPORT=streamable-http` |

### EBS instances

One deployment can reach several EBS databases. `EBSMCP_EBS_INSTANCES` is
single-line JSON — see [ebs-instances.sample.env](ebs-instances.sample.env) for the annotated shape.

```json
{"PROD": {"dsn": "host:1521/EBSPROD", "user": "ebsmcp_ro", "password": "..."}}
```

Instance names are arbitrary; use whatever the client calls their
environments. They match case-insensitively. The older singular
`EBS_DB_DSN`/`EBS_DB_USER`/`EBS_DB_PASSWORD` still works and is treated as one
instance named after the deployment's own environment.

Note that `EBSMCP_ENVIRONMENT` (`dev`/`test`/`uat`/`prod`) is a *different
axis* — it is which deploy stage this process serves, not which database it
reaches. One dev-stage deployment can reach a PROD EBS instance.

### Entra ID

Auth activates on its own once all three of these are set — there is no
separate enable flag:

| Variable | Meaning |
|---|---|
| `EBSMCP_ENTRA_TENANT_ID` | The tenant whose tokens are trusted |
| `EBSMCP_ENTRA_AUDIENCE` | API app registration's client ID; tokens must carry it as `aud` |
| `EBSMCP_RESOURCE_SERVER_URL` | Where this server is reachable — the OAuth resource identifier |
| `EBSMCP_ENTRA_SUBJECT_CLAIM` | Which claim becomes the subject. Default `preferred_username` |

Until then the server keeps using `EBSMCP_DEV_IDENTITY_SUBJECT`. Auth only
applies to `streamable-http` — stdio has no token to verify.

> On `EBSMCP_ENTRA_SUBJECT_CLAIM`: the default `preferred_username` (a UPN)
> matches the examples throughout this project, but a UPN can be reassigned
> after a rename. For a real deployment, `oid` — the immutable object ID — is
> the safer subject.

### DNS-rebinding protection

`EBSMCP_ALLOWED_HOSTS` and `EBSMCP_ALLOWED_ORIGINS` both default to empty,
which **rejects every request**. That is deliberate and fail-closed. A
streamable-http deployment must set them:

```bash
EBSMCP_ALLOWED_HOSTS='["mcp.client.com"]'
EBSMCP_ALLOWED_ORIGINS='["https://mcp.client.com"]'
```

---

## User onboarding

Nobody gets access by existing in Entra. Access comes from an **identity
mapping** created by an administrator, and mappings are what the MCP server
resolves on every call.

### Personas

There are two shapes of mapping, and they behave differently on purpose.

| | **Functional** (`ebs`, `fusion`) | **DBA** (`ebs_dba`) |
|---|---|---|
| Backend account | Required — an `FND_USER.USER_NAME` | Not required |
| Domain | Required — finance, scm, hcm… | Not applicable |
| Org ID scope | Required — the access boundary | **None. All-or-nothing.** |
| Mapped role | A real EBS responsibility | A descriptive label only |
| Grants | Data within those Org IDs | The full DBA toolset |

The DBA persona is not a third backend system — it is still EBS. But DBA work
(tablespaces, ADOP, backups) is not Org-scoped in any meaningful way, so an
`ebs_dba` mapping always resolves to zero Org IDs and skips the entitlement
intersection entirely. Its `mapped_role` is documentation, not a permission —
"Senior DBA — patching access" grants exactly what any other DBA mapping
grants.

Both database CHECK constraints and the API enforce this split, so a
functional mapping can never be saved without a username and domain.

### Onboarding a user

1. **Bootstrap an admin account.** A fresh deployment has **no admin account
   at all** — the `admin_accounts` table is created empty and nothing is
   seeded, so no sign-in works until you create the first account here. This
   is deliberate: a shipped default would make every deployment guessable, and
   the app's runtime role holds only `SELECT` on `admin_accounts`, so the
   running service cannot create or modify admins even in principle. Creation
   is a separate, out-of-band step, run with the same privileged credential
   used for migrations.

   **Docker Compose (the usual case)** — run the script inside the
   `management-api` container, which already has Python, the dependencies, and
   a route to Postgres, so nothing needs installing on the host:

   ```bash
   docker compose exec \
     -e IDENTITY_DB_URL=postgresql+psycopg://postgres:postgres@postgres:5432/ebsmcp_identity \
     management-api python scripts/create_admin.py --username admin@corp.com
   ```

   **Running the API directly on the host** instead:

   ```bash
   cd management-api
   IDENTITY_DB_URL=postgresql+psycopg://postgres:postgres@localhost:5433/ebsmcp_identity \
     python scripts/create_admin.py --username admin@corp.com
   ```

   Note the host form uses port **5433** — the port Compose publishes Postgres
   on — while the in-container form uses the internal **5432**. In a real
   deployment, point `IDENTITY_DB_URL` at that environment's own database with
   its own credentials, not the compose defaults shown here.

   The password is prompted for, never taken as an argument, so it stays out
   of shell history and process listings. Use `--reset-password` to change an
   existing account's password (for example, if you forget it — hashes cannot
   be recovered, only reset).

2. **Sign in** at the admin console, or `POST /auth/login` for a 12-hour
   session token.

3. **Look up what the user actually has in EBS.** Do not hand-type
   responsibilities or Org IDs:

   ```
   GET /ebs-lookup/users/{username}/responsibilities
   GET /ebs-lookup/responsibilities/{app_id}/{resp_id}/organizations
   ```

   Org IDs come from the responsibility's own MO: Security Profile setup. The
   mapping records that they were `resolved_from_source`; an admin may narrow
   the list afterward, and that is recorded as `manually_overridden`.

4. **Create the mapping** — `POST /identity-mappings` — with the subject,
   environment, target system, resolved role, and Org ID scope. Optionally
   restrict which EBS instances it may reach.

5. **Verify.** Have the user call `list_ebs_instances`. An unmapped caller
   gets a `no_identity_mapping` audit record and an error, not an empty result.

### Offboarding

**Close a mapping; never delete it.** `POST /identity-mappings/{id}/close`
sets `effective_end_date`, which ends access while keeping every
`created_by`/`updated_by` reference in the audit trail meaningful.

Similarly, disable an admin with `scripts/remove_admin.py` rather than
deleting the row — the default deactivates (`is_active = false`), which
blocks sign-in immediately while keeping the `created_by`/`updated_by`
references that admin left on every mapping they touched. The script also
lists accounts, reactivates one, and hard-deletes with `--delete` (which
breaks that history, so it is not the default), and it refuses to remove
the last active admin so you cannot lock yourself out:

```bash
docker compose exec \
  -e IDENTITY_DB_URL=postgresql+psycopg://postgres:postgres@postgres:5432/ebsmcp_identity \
  management-api python scripts/remove_admin.py --list
docker compose exec -e IDENTITY_DB_URL=... \
  management-api python scripts/remove_admin.py --username admin@corp.com
```

### The uniqueness rule

At most one *open* mapping per `(subject, environment, target_system, domain)`.
Two open mappings would make "which Org IDs apply" ambiguous — precisely the
ambiguity the entitlement filter exists to never have.

Domain is part of that key so one person can hold an open Finance mapping and
an open SCM mapping simultaneously. Since `domain` is NULL for every
`ebs_dba` row and SQL treats each NULL as distinct, the index coalesces NULL
to a sentinel — otherwise one person could quietly accumulate several open DBA
mappings.

---

## Security model

### Two authentication systems, deliberately separate

| | **End users** → MCP server | **Administrators** → management API |
|---|---|---|
| Identity | Entra ID (customer's tenant) | Local accounts in `admin_accounts` |
| Token | Entra JWT, RS256, verified via JWKS | Self-issued JWT, HS256, 12h TTL |
| Secret | None held — public keys only | `ADMIN_SESSION_SECRET` |
| Config | `entra_registrations` table | `management-api` env |

Admin access is intentionally **not** Entra-backed. This is the product's own
control plane: it should not go dark because a customer's tenant does, and a
licensable multi-tenant product cannot assume every deployment even has a
tenant to federate with. Passwords are bcrypt-hashed and only ever stored as
hashes — never in plaintext, not even transiently in a migration.

### What the platform guarantees

- **Read-only at every layer.** Tools issue `SELECT` only and are annotated
  `read_only_hint=True`. Point them at a read-only EBS account (`ebsmcp_ro`)
  so this is enforced by the database, not by convention.
- **Scope can only narrow.** Enforced centrally in `EntitlementFilter`.
- **Absence of a mapping is denial.** A missing mapping raises `LookupError`,
  never "no restriction".
- **Empty means deny — except where it can't.** `allowed_org_ids=()` is a
  denial. `allowed_instances` uses the opposite convention: `None` means "not
  instance-scoped", and an empty tuple is an explicit zero-instance grant. The
  distinction exists because reusing the org convention would have silently
  revoked EBS access from every mapping that predates instance scoping.
- **Denials are visible.** `EntitlementDenied` subclasses `ToolError`, so a
  client sees `is_error=True` with a real reason rather than an empty result
  that looks like "no data".
- **Fail-closed transport defaults.** Empty host/origin allowlists reject
  everything until configured.
- **Environment isolation.** One deployment serves exactly one deploy stage,
  with its own secrets, service accounts, and audit store.

### Before you go to production

Two things in this repo are dev defaults, not production settings:

- `management-api` sets CORS `allow_origins=["*"]` for the Vite dev server —
  see [app/main.py](management-api/app/main.py). Narrow it, or front both services through one
  origin.
- [docker-compose.yml](docker-compose.yml) uses `postgres/postgres` and a shared superuser
  connection string. A real deployment wants per-service least-privilege
  roles; the `*-service` `.env.example` files describe the intended split.

---

## Auditing

Every call produces exactly one `AuditRecord`, whatever the outcome:

| `status` | Meaning |
|---|---|
| `ok` | Completed |
| `denied` | Entitlement check rejected the requested scope |
| `no_identity_mapping` | Caller has no open mapping — an onboarding gap |
| `error` | Genuine fault |

`no_identity_mapping` is separated from `error` on purpose: an onboarding gap
and a system fault need different responses, and collapsing them hides a
signal that someone is trying to reach something they were never granted.

Each record carries a correlation ID, subject, tool name, environment, target
system, parameters, the **effective** Org IDs after filtering, the resolved
instance, and latency.

Records are written as JSON lines to stdout — which is what any log pipeline
(Fluent Bit, Vector) collects from a container first — and, when `AUDIT_DB_URL`
is set, inserted into the partitioned store in [audit-service/](audit-service/)
as a second sink on the same record shape, not a replacement. That second sink
is what the admin console's **Audit log** page reads; without it the page is
empty even though every call is still being audited to stdout.

The two sinks fail independently and stdout is the one that must not fail: a
write to the audit database is best-effort, and a failure is reported on stdout
rather than raised, because the record is already durable there and refusing
the tool call would turn an audit-store outage into an outage of the product.

> **Operational note:** `audit_log` is partitioned by month with a `DEFAULT`
> partition as a backstop, because a month with no partition would otherwise
> reject writes outright — an audit table must never stop accepting writes
> because provisioning fell behind. Provisioning future partitions is a
> recurring job (a scheduled task, or `pg_partman`), not something the
> migration does once.

---

## Development

```bash
cd mcp-server && pip install -e ".[dev]" && pytest      # 25 test modules
cd management-api && pip install -e ".[dev]" && pytest  # 7 test modules
cd admin-gui && npm install && npx playwright test      # e2e
```

`mcp-server/tests/test_sql_conventions.py` is a guardrail rather than a unit
test — it checks every tool's SQL against project-wide conventions, so a new
tool cannot quietly introduce a different query style.

### Adding a toolset

1. Write a module in `mcp-server/src/ebsmcp/tools/dba/` with a `_register`
   function, and wrap each tool body in `resolve_scoped_call` — that is what
   supplies identity, entitlement, routing, and audit.
2. Export a `ToolSet(name=..., register=_register)`.
3. Add it to the mount list in [server.py](mcp-server/src/ebsmcp/server.py).

Placeholder packages already exist for `finance/`, `hcm/`, `scm/`, and
`fusion/`.

### Schema changes

Schema lives in `identity-service` / `audit-service` as SQLAlchemy Core tables
with Alembic migrations — one definition per table, with dialect-specific
pieces tagged `.ddl_if(dialect=...)` so Postgres and Oracle cannot drift apart.

`mcp-server/src/ebsmcp/identity/tables.py` is a deliberate partial copy of the
identity schema — only the columns it reads. It is a *client* of that schema,
not its owner. **Keep it in sync by hand** when changing
`identity-service/db/models.py`.
