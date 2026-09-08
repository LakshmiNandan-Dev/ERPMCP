import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, getEntraConfig, putEntraConfig } from "../api/client";
import { useAdminIdentity } from "../auth/AdminIdentity";
import type { Environment, EntraRegistration } from "../types";

const EMPTY_FORM = { tenant_id: "", audience: "", subject_claim: "preferred_username", resource_server_url: "" };

function formatDate(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

export function EntraSettings() {
  const { subject: adminSubject } = useAdminIdentity();
  const [environment, setEnvironment] = useState<Environment>("prod");
  const [current, setCurrent] = useState<EntraRegistration | null>(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // Switching the environment dropdown quickly (or a fetch for the
  // previous environment still being in flight when a new one starts) can
  // let two requests race — without a guard, whichever resolves last wins
  // the UI state regardless of which was requested more recently. Caught
  // by an actual Playwright run: selecting "test" right after mount showed
  // "prod"'s data because its fetch happened to resolve second.
  const latestRequestId = useRef(0);

  const load = useCallback(async () => {
    const requestId = ++latestRequestId.current;
    setLoading(true);
    setError(null);
    setSaved(false);
    try {
      const config = await getEntraConfig(environment);
      if (latestRequestId.current !== requestId) return; // superseded by a newer request
      setCurrent(config);
      setForm(
        config
          ? {
              tenant_id: config.tenant_id,
              audience: config.audience,
              subject_claim: config.subject_claim,
              resource_server_url: config.resource_server_url,
            }
          : EMPTY_FORM,
      );
    } catch (err) {
      if (latestRequestId.current !== requestId) return;
      setError(err instanceof ApiError ? err.message : "Failed to load Entra configuration.");
    } finally {
      if (latestRequestId.current === requestId) setLoading(false);
    }
  }, [environment]);

  useEffect(() => {
    // Re-runs once adminSubject becomes available — see IdentityMappings'
    // identical guard for why: real MSAL sign-in's token acquisition is
    // async, so the first render can't assume a credential exists yet.
    if (!adminSubject) return;
    load();
  }, [load, adminSubject]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSaved(false);

    if (!adminSubject) {
      setError('Set "Acting as" above before saving — this is a trust-anchor setting, changes are attributed.');
      return;
    }

    setSaving(true);
    try {
      const result = await putEntraConfig(environment, form);
      setCurrent(result);
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save Entra configuration.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page">
      <div className="section-header">
        <h2>Entra ID configuration</h2>
        <label className="env-picker">
          Environment
          <select value={environment} onChange={(e) => setEnvironment(e.target.value as Environment)}>
            <option value="dev">dev</option>
            <option value="test">test</option>
            <option value="uat">uat</option>
            <option value="prod">prod</option>
          </select>
        </label>
      </div>

      <div className="callout-warning">
        This defines which Entra tenant and which tokens mcp-server trusts for {environment} — not ordinary app
        data. Saving does <strong>not</strong> take effect immediately: mcp-server only reads this at startup, so a
        restart is required before a change applies.
      </div>

      {loading ? (
        <p className="muted">Loading…</p>
      ) : (
        <form className="new-mapping-form" onSubmit={handleSubmit}>
          {current && (
            <p className="muted">
              Currently configured — created by {current.created_by} on {formatDate(current.created_at)}
              {current.updated_by && (
                <>
                  {" "}
                  · last updated by {current.updated_by} on {formatDate(current.updated_at)}
                </>
              )}
              .
            </p>
          )}
          {!current && <p className="muted">No Entra registration configured for {environment} yet.</p>}

          <div className="form-row">
            <label>
              Tenant ID
              <input
                type="text"
                required
                placeholder="00000000-0000-0000-0000-000000000000"
                value={form.tenant_id}
                onChange={(e) => setForm((f) => ({ ...f, tenant_id: e.target.value }))}
              />
            </label>
            <label>
              Audience (API app client ID)
              <input
                type="text"
                required
                placeholder="11111111-1111-1111-1111-111111111111"
                value={form.audience}
                onChange={(e) => setForm((f) => ({ ...f, audience: e.target.value }))}
              />
            </label>
          </div>

          <div className="form-row">
            <label>
              Subject claim
              <select
                value={form.subject_claim}
                onChange={(e) => setForm((f) => ({ ...f, subject_claim: e.target.value }))}
              >
                <option value="preferred_username">preferred_username (UPN/email)</option>
                <option value="oid">oid (immutable object ID)</option>
              </select>
            </label>
            <label>
              Resource server URL
              <input
                type="url"
                required
                placeholder="https://ebsmcp.corp.com/"
                value={form.resource_server_url}
                onChange={(e) => setForm((f) => ({ ...f, resource_server_url: e.target.value }))}
              />
            </label>
          </div>

          {error && <p className="form-error">{error}</p>}
          {saved && <p className="form-success">Saved. Restart mcp-server for this to take effect.</p>}

          <button type="submit" className="submit-btn" disabled={saving}>
            {saving ? "Saving…" : current ? "Update configuration" : "Create configuration"}
          </button>
        </form>
      )}
    </div>
  );
}
