import type {
  AssignedOrganization,
  Branding,
  BrandingInput,
  AssignedResponsibility,
  AuditLogEntry,
  Environment,
  EntraRegistration,
  EntraRegistrationInput,
  IdentityMapping,
  IdentityMappingInput,
  TargetSystem,
} from "../types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8010";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// Whichever proves who's calling — X-Admin-Subject in stub mode,
// Authorization: Bearer <token> once real Entra sign-in is configured
// (see src/auth/). Set once by whichever auth mode is active and
// attached to every request from here, the same way a browser attaches
// a session's credentials automatically rather than every call site
// threading them through by hand. GETs need this now too: management-api
// gates reads the same way it gates writes.
let authHeaders: Record<string, string> = {};

export function setAuthHeaders(headers: Record<string, string>): void {
  authHeaders = headers;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...authHeaders, ...init?.headers },
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => null);
    // FastAPI's own validation errors (422) put an array of per-field
    // problems in `detail`, not a string — every other error path here
    // (401/404/etc, see routers/auth.py) puts a plain string. Without this,
    // an array ends up coerced through Error's default String(message) and
    // shows the user "[object Object]" instead of e.g. "String should have
    // at least 1 character".
    const detail = body?.detail;
    const message = Array.isArray(detail)
      ? detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join("; ") || `Request failed with status ${resp.status}`
      : (detail ?? `Request failed with status ${resp.status}`);
    throw new ApiError(resp.status, message);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

export interface ListIdentityMappingsParams {
  entraSubject?: string;
  environment?: Environment;
  targetSystem?: TargetSystem;
  domain?: string;
  includeClosed?: boolean;
}

export function listIdentityMappings(params: ListIdentityMappingsParams = {}): Promise<IdentityMapping[]> {
  const qs = new URLSearchParams();
  if (params.entraSubject) qs.set("entra_subject", params.entraSubject);
  if (params.environment) qs.set("environment", params.environment);
  if (params.targetSystem) qs.set("target_system", params.targetSystem);
  if (params.domain) qs.set("domain", params.domain);
  if (params.includeClosed) qs.set("include_closed", "true");
  const suffix = qs.toString() ? `?${qs}` : "";
  return request(`/identity-mappings${suffix}`);
}

export function createIdentityMapping(body: IdentityMappingInput): Promise<IdentityMapping> {
  return request("/identity-mappings", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function closeIdentityMapping(id: number): Promise<IdentityMapping> {
  return request(`/identity-mappings/${id}/close`, { method: "POST" });
}

export interface ListAuditLogParams {
  subject?: string;
  environment?: string;
  toolName?: string;
  instance?: string;
  status?: string;
  /** ISO timestamps. The console sends start-of-day / end-of-day for the
   *  dates picked, so the server's >= / <= mean what the operator expects. */
  since?: string;
  until?: string;
  limit?: number;
}

export function listAuditLog(params: ListAuditLogParams = {}): Promise<AuditLogEntry[]> {
  const qs = new URLSearchParams();
  if (params.subject) qs.set("subject", params.subject);
  if (params.environment) qs.set("environment", params.environment);
  if (params.toolName) qs.set("tool_name", params.toolName);
  if (params.instance) qs.set("instance", params.instance);
  if (params.status) qs.set("status", params.status);
  if (params.since) qs.set("since", params.since);
  if (params.until) qs.set("until", params.until);
  qs.set("limit", String(params.limit ?? 50));
  return request(`/audit-log?${qs}`);
}

export async function getEntraConfig(environment: Environment): Promise<EntraRegistration | null> {
  try {
    return await request(`/entra-config/${environment}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export function getAssignedResponsibilities(username: string): Promise<AssignedResponsibility[]> {
  return request(`/ebs-lookup/users/${encodeURIComponent(username)}/responsibilities`);
}

export function getAssignedOrganizations(
  applicationId: number,
  responsibilityId: number,
): Promise<AssignedOrganization[]> {
  return request(`/ebs-lookup/responsibilities/${applicationId}/${responsibilityId}/organizations`);
}

// The fixed domain list for the one flow with no FND_APPLICATION to
// derive a domain from — a Fusion mapping, entered manually.
export function getKnownDomains(): Promise<string[]> {
  return request("/ebs-lookup/domains");
}

export function putEntraConfig(environment: Environment, body: EntraRegistrationInput): Promise<EntraRegistration> {
  return request(`/entra-config/${environment}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  subject: string;
}

export function login(username: string, password: string): Promise<LoginResponse> {
  return request("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

// Unauthenticated by design — the header renders before sign-in, so this is
// the one read that must work with no credentials. authHeaders is simply
// empty at that point; no special-casing needed here.
export function getBranding(): Promise<Branding> {
  return request("/branding");
}

export function putBranding(body: BrandingInput): Promise<Branding> {
  return request("/branding", { method: "PUT", body: JSON.stringify(body) });
}
