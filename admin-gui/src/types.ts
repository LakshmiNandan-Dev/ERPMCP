// Mirrors management-api/app/schemas/identity.py and audit.py exactly.
// Kept as a hand-written match rather than generated from the OpenAPI
// schema for this first pass — worth revisiting with an OpenAPI generator
// once the API surface stabilizes, since hand-matching two languages is
// exactly the kind of thing that quietly drifts apart over time.

export type Environment = "dev" | "test" | "uat" | "prod";
// ebs_dba is a persona, not a third backend — DBA tool access isn't
// Org-scoped and doesn't need an FND_USER account. See
// management-api/app/schemas/identity.py's TargetSystem comment.
export type TargetSystem = "ebs" | "fusion" | "ebs_dba";
export type ResolutionSource = "resolved_from_source" | "manually_overridden";
export type AuditStatus = "ok" | "denied" | "no_identity_mapping" | "error";

export interface OrgScope {
  id: number;
  org_id: string;
  org_name: string | null;
  resolved_from_source: boolean;
}

export interface OrgScopeInput {
  org_id: string;
  org_name?: string;
  resolved_from_source: boolean;
}

export interface IdentityMapping {
  id: number;
  entra_subject: string;
  environment: Environment;
  target_system: TargetSystem;
  // null for ebs_dba mappings — see TargetSystem's comment.
  target_username: string | null;
  // Functional pillar (finance, scm, manufacturing, hcm, ...) this
  // mapping belongs to — null for ebs_dba, same reasoning as
  // target_username. See app/ebs/domains.py.
  domain: string | null;
  mapped_role: string;
  resolution_source: ResolutionSource;
  effective_start_date: string;
  effective_end_date: string | null;
  created_at: string;
  created_by: string;
  updated_at: string | null;
  updated_by: string | null;
  org_scope: OrgScope[];
}

export interface IdentityMappingInput {
  entra_subject: string;
  environment: Environment;
  target_system: TargetSystem;
  // Omit or leave undefined for ebs_dba mappings.
  target_username?: string;
  // Required for ebs/fusion; omit for ebs_dba. For ebs, relay exactly
  // the selected responsibility's `domain` — see AssignedResponsibility.
  domain?: string;
  mapped_role: string;
  effective_start_date: string;
  org_scope: OrgScopeInput[];
}

// Mirrors management-api/app/schemas/ebs_lookup.py.
export interface AssignedResponsibility {
  responsibility_id: number;
  application_id: number;
  responsibility_name: string;
  // Functional pillar this responsibility's Application belongs to —
  // resolved server-side from application_id, not typed by the admin.
  domain: string;
}

export interface AssignedOrganization {
  org_id: string;
  org_name: string;
  source: "security_profile" | "operating_unit";
}

export interface EntraRegistration {
  id: number;
  environment: Environment;
  tenant_id: string;
  audience: string;
  subject_claim: string;
  resource_server_url: string;
  created_at: string;
  created_by: string;
  updated_at: string | null;
  updated_by: string | null;
}

export interface EntraRegistrationInput {
  tenant_id: string;
  audience: string;
  subject_claim: string;
  resource_server_url: string;
}

export interface AuditLogEntry {
  id: number;
  correlation_id: string;
  occurred_at: string;
  tool_name: string;
  subject: string;
  environment: string;
  target_system: string;
  status: AuditStatus;
  instance: string | null;
  effective_org_ids: string[] | null;
  params: Record<string, unknown> | null;
  error_message: string | null;
  latency_ms: number;
}

// Mirrors management-api/app/schemas/branding.py. Every field is nullable:
// an unconfigured deployment is a normal state, and the header falls back to
// the product default rather than treating it as an error.
export interface Branding {
  site_name: string | null;
  company_name: string | null;
  logo_data_uri: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface BrandingInput {
  site_name: string | null;
  company_name: string | null;
  logo_data_uri: string | null;
}

// Mirrors management-api's GET /deployment. Which deploy stage this
// deployment serves — NOT which EBS database it reaches; see the README on
// those being different axes. Identity mappings only resolve when their
// environment matches this.
export interface DeploymentInfo {
  environment: string;
}
