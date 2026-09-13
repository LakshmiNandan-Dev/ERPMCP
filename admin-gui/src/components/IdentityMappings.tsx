import { useCallback, useEffect, useState } from "react";
import { ApiError, closeIdentityMapping, getDeployment, listIdentityMappings } from "../api/client";
import { useAdminIdentity } from "../auth/AdminIdentity";
import type { IdentityMapping } from "../types";
import { NewMappingForm } from "./NewMappingForm";

function formatDate(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

export function IdentityMappings() {
  const { subject: adminSubject } = useAdminIdentity();
  const [mappings, setMappings] = useState<IdentityMapping[]>([]);
  const [includeClosed, setIncludeClosed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [closingId, setClosingId] = useState<number | null>(null);
  // Fetched once here and passed down, rather than letting the form fetch it
  // too — two components racing the same call could disagree mid-render.
  // Unauthenticated, so it does not wait on adminSubject. A failure leaves it
  // null and simply suppresses the warnings, never blocks the page.
  const [liveEnvironment, setLiveEnvironment] = useState<string | null>(null);

  useEffect(() => {
    getDeployment()
      .then((d) => setLiveEnvironment(d.environment))
      .catch(() => setLiveEnvironment(null));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setMappings(await listIdentityMappings({ includeClosed }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load identity mappings.");
    } finally {
      setLoading(false);
    }
  }, [includeClosed]);

  useEffect(() => {
    // Re-runs once adminSubject becomes available: real MSAL sign-in
    // acquires its token asynchronously, so the first render's fetch
    // would otherwise fail once, permanently, before any credential
    // exists to retry with.
    if (!adminSubject) return;
    load();
  }, [load, adminSubject]);

  const handleClose = async (id: number) => {
    if (!adminSubject) {
      setError('Set "Acting as" above before closing a mapping.');
      return;
    }
    setClosingId(id);
    try {
      await closeIdentityMapping(id);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to close the mapping.");
    } finally {
      setClosingId(null);
    }
  };

  return (
    <div className="page">
      <NewMappingForm onCreated={load} liveEnvironment={liveEnvironment} />

      <div className="section-header">
        <h2>Identity mappings</h2>
        <label className="checkbox-label">
          <input type="checkbox" checked={includeClosed} onChange={(e) => setIncludeClosed(e.target.checked)} />
          include closed
        </label>
      </div>

      {error && <p className="form-error">{error}</p>}
      {loading ? (
        <p className="muted">Loading…</p>
      ) : mappings.length === 0 ? (
        <p className="muted">No identity mappings yet.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Subject</th>
                <th>Env</th>
                <th>System</th>
                <th>Account</th>
                <th>Domain</th>
                <th>Role</th>
                <th>Org scope</th>
                <th>Source</th>
                <th>Effective</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {mappings.map((m) => {
                const isOpen = m.effective_end_date === null;
                return (
                  <tr key={m.id} className={isOpen ? undefined : "row-closed"}>
                    <td>{m.entra_subject}</td>
                    <td>
                      <span className="pill">{m.environment}</span>
                      {liveEnvironment && m.environment !== liveEnvironment && (
                        <span
                          className="pill pill-warn"
                          title={`This deployment resolves mappings for '${liveEnvironment}' only — this one will never match, and the user will be denied with "no identity mapping".`}
                        >
                          inactive stage
                        </span>
                      )}
                    </td>
                    <td>{m.target_system === "ebs_dba" ? "EBS — DBA" : m.target_system.toUpperCase()}</td>
                    <td>
                      {m.target_username ? <code>{m.target_username}</code> : <span className="muted">—</span>}
                    </td>
                    <td>{m.domain ? <span className="pill">{m.domain}</span> : <span className="muted">—</span>}</td>
                    <td>{m.mapped_role}</td>
                    <td>
                      {m.target_system === "ebs_dba" ? (
                        <span className="pill pill-org">All DBA tools</span>
                      ) : (
                        m.org_scope.map((s) => (
                          <span className="pill pill-org" key={s.id} title={s.org_name ?? undefined}>
                            {s.org_id}
                          </span>
                        ))
                      )}
                    </td>
                    <td className="muted">
                      {m.resolution_source === "resolved_from_source" ? "resolved" : "manual"}
                    </td>
                    <td className="muted">
                      {formatDate(m.effective_start_date)}
                      {" → "}
                      {isOpen ? "open" : formatDate(m.effective_end_date)}
                    </td>
                    <td>
                      {isOpen && (
                        <button
                          type="button"
                          className="close-btn"
                          onClick={() => handleClose(m.id)}
                          disabled={closingId === m.id}
                        >
                          {closingId === m.id ? "Closing…" : "Close"}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
