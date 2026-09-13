import { useEffect, useState } from "react";
import {
  ApiError,
  createIdentityMapping,
  getAssignedOrganizations,
  getAssignedResponsibilities,
  getKnownDomains,
} from "../api/client";
import { useAdminIdentity } from "../auth/AdminIdentity";
import type {
  AssignedOrganization,
  AssignedResponsibility,
  Environment,
  IdentityMapping,
  OrgScopeInput,
  TargetSystem,
} from "../types";

interface Props {
  onCreated: (mapping: IdentityMapping) => void;
  // The deploy stage this deployment actually serves, from GET /deployment.
  // null while it is still loading, or if the call failed — in which case no
  // warning is shown rather than a wrong one.
  liveEnvironment: string | null;
}

const emptyScope = (): OrgScopeInput => ({ org_id: "", org_name: "", resolved_from_source: false });

function responsibilityKey(r: AssignedResponsibility): string {
  return `${r.application_id}:${r.responsibility_id}`;
}

export function NewMappingForm({ onCreated, liveEnvironment }: Props) {
  const { subject: adminSubject } = useAdminIdentity();
  const [entraSubject, setEntraSubject] = useState("");
  const [environment, setEnvironment] = useState<Environment>("dev");
  const [targetSystem, setTargetSystem] = useState<TargetSystem>("ebs");
  const [targetUsername, setTargetUsername] = useState("");
  const [effectiveStartDate, setEffectiveStartDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // EBS lookup flow — pick the username, see what's actually assigned to
  // it in EBS, pick a responsibility, see exactly which orgs it grants.
  // Nothing here is typed; org_scope only ever narrows this list.
  const [lookingUp, setLookingUp] = useState(false);
  const [responsibilities, setResponsibilities] = useState<AssignedResponsibility[]>([]);
  const [selectedRespKey, setSelectedRespKey] = useState("");
  const [loadingOrgs, setLoadingOrgs] = useState(false);
  const [availableOrgs, setAvailableOrgs] = useState<AssignedOrganization[]>([]);
  const [selectedOrgIds, setSelectedOrgIds] = useState<Set<string>>(new Set());

  // Fusion has no equivalent lookup built yet — falls back to manual entry.
  // domain still isn't free text, though: no FND_APPLICATION to derive it
  // from here, so it's picked from the same fixed list an EBS lookup
  // would resolve to (see app/ebs/domains.py), not typed.
  const [fusionMappedRole, setFusionMappedRole] = useState("");
  const [fusionOrgScope, setFusionOrgScope] = useState<OrgScopeInput[]>([emptyScope()]);
  const [fusionDomain, setFusionDomain] = useState("");
  const [knownDomains, setKnownDomains] = useState<string[]>([]);

  useEffect(() => {
    // Re-runs once adminSubject becomes available, not just on mount:
    // this fetch fires before real MSAL sign-in's async token acquisition
    // resolves (or before "Acting as" is filled in stub mode), so without
    // this dependency it fails once, permanently, with no way to recover
    // short of a full page reload.
    if (!adminSubject) return;
    getKnownDomains()
      .then(setKnownDomains)
      .catch(() => setKnownDomains([]));
  }, [adminSubject]);

  // ebs_dba: a persona, not a backend — no FND_USER, no Org scope. Access
  // is all-or-nothing (the full DBA toolset), so there's nothing to look
  // up or select here beyond a descriptive label.
  const [dbaRoleLabel, setDbaRoleLabel] = useState("");

  const resetLookupState = () => {
    setResponsibilities([]);
    setSelectedRespKey("");
    setAvailableOrgs([]);
    setSelectedOrgIds(new Set());
  };

  const handleLookupUser = async () => {
    setError(null);
    resetLookupState();
    if (!targetUsername.trim()) {
      setError("Enter an EBS username to look up.");
      return;
    }
    setLookingUp(true);
    try {
      setResponsibilities(await getAssignedResponsibilities(targetUsername.trim()));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to look up that EBS user.");
    } finally {
      setLookingUp(false);
    }
  };

  const handleSelectResponsibility = async (key: string) => {
    setSelectedRespKey(key);
    setAvailableOrgs([]);
    setSelectedOrgIds(new Set());
    if (!key) return;

    const resp = responsibilities.find((r) => responsibilityKey(r) === key);
    if (!resp) return;

    setLoadingOrgs(true);
    setError(null);
    try {
      const orgs = await getAssignedOrganizations(resp.application_id, resp.responsibility_id);
      setAvailableOrgs(orgs);
      setSelectedOrgIds(new Set(orgs.map((o) => o.org_id))); // all granted, all selected by default
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to resolve organizations for that responsibility.");
    } finally {
      setLoadingOrgs(false);
    }
  };

  const toggleOrg = (orgId: string) => {
    setSelectedOrgIds((prev) => {
      const next = new Set(prev);
      if (next.has(orgId)) next.delete(orgId);
      else next.add(orgId); // re-checking only ever restores something already granted — never adds new
      return next;
    });
  };

  const resetForm = () => {
    setEntraSubject("");
    setTargetUsername("");
    resetLookupState();
    setFusionMappedRole("");
    setFusionOrgScope([emptyScope()]);
    setFusionDomain("");
    setDbaRoleLabel("");
  };

  const selectedResponsibility = responsibilities.find((r) => responsibilityKey(r) === selectedRespKey);

  // mcp-server resolves mappings for its own stage only. A mapping created
  // for any other stage saves, lists, and reads as active — then never
  // matches, surfacing to the user as no_identity_mapping while the admin is
  // looking at what appears to be a working grant.
  const offStage = liveEnvironment !== null && environment !== liveEnvironment;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!adminSubject) {
      setError('Set "Acting as" above before creating a mapping — it becomes created_by on the record.');
      return;
    }
    if (targetSystem !== "ebs_dba" && !targetUsername.trim()) {
      setError("Target username is required (only a DBA mapping may omit it).");
      return;
    }

    let mappedRole: string;
    let orgScope: OrgScopeInput[];
    let domain: string | undefined;

    if (targetSystem === "ebs_dba") {
      if (!dbaRoleLabel.trim()) {
        setError("Enter a descriptive DBA role label.");
        return;
      }
      mappedRole = dbaRoleLabel;
      orgScope = []; // all-or-nothing: a DBA mapping grants the full toolset, nothing to select
      domain = undefined; // not domain-scoped
    } else if (targetSystem === "ebs") {
      if (!selectedResponsibility) {
        setError("Look up the EBS user and select a responsibility.");
        return;
      }
      if (selectedOrgIds.size === 0) {
        setError("At least one Org ID must be selected.");
        return;
      }
      mappedRole = selectedResponsibility.responsibility_name;
      orgScope = availableOrgs
        .filter((o) => selectedOrgIds.has(o.org_id))
        .map((o) => ({ org_id: o.org_id, org_name: o.org_name, resolved_from_source: true }));
      domain = selectedResponsibility.domain; // relayed, not typed — see AssignedResponsibility
    } else {
      const cleanScope = fusionOrgScope.filter((row) => row.org_id.trim().length > 0);
      if (!fusionMappedRole.trim() || cleanScope.length === 0 || !fusionDomain) {
        setError("Fusion role, a domain, and at least one Org ID are required.");
        return;
      }
      mappedRole = fusionMappedRole;
      orgScope = cleanScope;
      domain = fusionDomain;
    }

    setSubmitting(true);
    try {
      const created = await createIdentityMapping({
        entra_subject: entraSubject,
        environment,
        target_system: targetSystem,
        target_username: targetSystem === "ebs_dba" ? undefined : targetUsername.trim(),
        domain,
        mapped_role: mappedRole,
        effective_start_date: new Date(effectiveStartDate).toISOString(),
        org_scope: orgScope,
      });
      onCreated(created);
      resetForm();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong creating the mapping.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className="new-mapping-form" onSubmit={handleSubmit}>
      <h2>New mapping</h2>

      <div className="form-row">
        <label>
          Entra subject
          <input
            type="email"
            required
            placeholder="j.doe@corp.com"
            value={entraSubject}
            onChange={(e) => setEntraSubject(e.target.value)}
          />
        </label>
        <label>
          Environment
          <select value={environment} onChange={(e) => setEnvironment(e.target.value as Environment)}>
            <option value="dev">dev</option>
            <option value="test">test</option>
            <option value="uat">uat</option>
            <option value="prod">prod</option>
          </select>
          {liveEnvironment && !offStage && (
            <small className="muted">This deployment serves {liveEnvironment}.</small>
          )}
          {offStage && (
            <small className="form-warning">
              This deployment serves <strong>{liveEnvironment}</strong>. A mapping created for{" "}
              <strong>{environment}</strong> will be saved and listed as active, but will never resolve —
              the user will be denied with &ldquo;no identity mapping&rdquo;. Create it only if you are
              pre-staging for a future promotion.
            </small>
          )}
        </label>
        <label>
          Target system
          <select
            value={targetSystem}
            onChange={(e) => {
              setTargetSystem(e.target.value as TargetSystem);
              setTargetUsername("");
              resetLookupState();
            }}
          >
            <option value="ebs">EBS</option>
            <option value="fusion">Fusion</option>
            <option value="ebs_dba">EBS — DBA</option>
          </select>
        </label>
      </div>

      {targetSystem === "ebs_dba" ? (
        <div className="form-row">
          <label>
            DBA role label
            <input
              type="text"
              required
              placeholder="Senior DBA — patching access"
              value={dbaRoleLabel}
              onChange={(e) => setDbaRoleLabel(e.target.value)}
            />
          </label>
          <p className="muted dba-note">
            Grants the full DBA toolset — not Org-scoped, no FND_USER lookup, nothing further to select.
          </p>
        </div>
      ) : targetSystem === "ebs" ? (
        <>
          <div className="form-row lookup-row">
            <label>
              EBS username (FND_USER)
              <input
                type="text"
                required
                placeholder="JDOE"
                value={targetUsername}
                onChange={(e) => setTargetUsername(e.target.value)}
              />
            </label>
            <button type="button" className="lookup-btn" onClick={handleLookupUser} disabled={lookingUp}>
              {lookingUp ? "Looking up…" : "Look up user"}
            </button>
          </div>

          {responsibilities.length > 0 && (
            <div className="form-row">
              <label>
                Responsibility
                <select value={selectedRespKey} onChange={(e) => handleSelectResponsibility(e.target.value)}>
                  <option value="">Select a responsibility…</option>
                  {responsibilities.map((r) => (
                    <option key={responsibilityKey(r)} value={responsibilityKey(r)}>
                      {r.responsibility_name} ({r.domain})
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}

          {selectedResponsibility && (
            <p className="muted">
              Domain: <span className="pill">{selectedResponsibility.domain}</span> — resolved from this
              responsibility's Application, not chosen here.
            </p>
          )}

          {loadingOrgs && <p className="muted">Resolving assigned organizations…</p>}

          {availableOrgs.length > 0 && (
            <fieldset className="org-scope-fieldset">
              <legend>
                Org ID / BU scope
                <span className="scope-note">
                  resolved from {selectedResponsibility?.responsibility_name}'s{" "}
                  {availableOrgs[0].source === "security_profile" ? "MO: Security Profile" : "MO: Operating Unit"} —
                  uncheck to narrow, nothing can be added outside this list
                </span>
              </legend>
              {availableOrgs.map((org) => (
                <label className="org-checkbox-row" key={org.org_id}>
                  <input
                    type="checkbox"
                    checked={selectedOrgIds.has(org.org_id)}
                    onChange={() => toggleOrg(org.org_id)}
                  />
                  <span className="pill pill-org">{org.org_id}</span>
                  {org.org_name}
                </label>
              ))}
            </fieldset>
          )}
        </>
      ) : (
        <>
          <div className="form-row">
            <label>
              EBS/Fusion username
              <input
                type="text"
                required
                placeholder="jdoe"
                value={targetUsername}
                onChange={(e) => setTargetUsername(e.target.value)}
              />
            </label>
            <label>
              Fusion role
              <input
                type="text"
                required
                placeholder="AP Invoice Reviewer"
                value={fusionMappedRole}
                onChange={(e) => setFusionMappedRole(e.target.value)}
              />
            </label>
            <label>
              Domain
              <select value={fusionDomain} onChange={(e) => setFusionDomain(e.target.value)} required>
                <option value="">Select a domain…</option>
                {knownDomains.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <fieldset className="org-scope-fieldset">
            <legend>
              Business Unit scope
              <span className="scope-note">typed manually — automatic Fusion resolution isn't built yet</span>
            </legend>
            {fusionOrgScope.map((row, i) => (
              <div className="org-scope-row" key={i}>
                <input
                  type="text"
                  placeholder="BU ID"
                  required
                  value={row.org_id}
                  onChange={(e) =>
                    setFusionOrgScope((rows) => rows.map((r, idx) => (idx === i ? { ...r, org_id: e.target.value } : r)))
                  }
                />
                <input
                  type="text"
                  placeholder="Label (e.g. US1 Business Unit)"
                  value={row.org_name}
                  onChange={(e) =>
                    setFusionOrgScope((rows) =>
                      rows.map((r, idx) => (idx === i ? { ...r, org_name: e.target.value } : r)),
                    )
                  }
                />
                <button
                  type="button"
                  className="remove-row-btn"
                  onClick={() => setFusionOrgScope((rows) => rows.filter((_, idx) => idx !== i))}
                  disabled={fusionOrgScope.length === 1}
                  aria-label="Remove this Org ID"
                >
                  &times;
                </button>
              </div>
            ))}
            <button type="button" className="add-row-btn" onClick={() => setFusionOrgScope((rows) => [...rows, emptyScope()])}>
              + Add Org ID
            </button>
          </fieldset>
        </>
      )}

      <div className="form-row">
        <label>
          Effective start date
          <input
            type="date"
            required
            value={effectiveStartDate}
            onChange={(e) => setEffectiveStartDate(e.target.value)}
          />
        </label>
      </div>

      {error && <p className="form-error">{error}</p>}

      <button type="submit" className="submit-btn" disabled={submitting}>
        {submitting ? "Creating…" : "Create mapping"}
      </button>
    </form>
  );
}
