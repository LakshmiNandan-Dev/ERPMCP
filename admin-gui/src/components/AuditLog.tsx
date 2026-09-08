import { Fragment, useCallback, useEffect, useState } from "react";
import { ApiError, listAuditLog } from "../api/client";
import type { AuditLogEntry, AuditStatus } from "../types";

const STATUS_LABEL: Record<AuditStatus, string> = {
  ok: "ok",
  denied: "denied",
  no_identity_mapping: "no mapping",
  error: "error",
};

function StatusBadge({ status }: { status: AuditStatus }) {
  return <span className={`status-badge status-${status}`}>{STATUS_LABEL[status]}</span>;
}

export function AuditLog() {
  const [entries, setEntries] = useState<AuditLogEntry[]>([]);
  const [subject, setSubject] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setEntries(await listAuditLog({ subject: subject || undefined, status: status || undefined }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load the audit log.");
    } finally {
      setLoading(false);
    }
  }, [subject, status]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="page">
      <div className="section-header">
        <h2>Audit log</h2>
        <p className="muted audit-note">
          Read-only — this API connects to the audit database with a role that can only SELECT, enforced by
          Postgres, not just by this page never offering a write action.
        </p>
      </div>

      <form
        className="filter-bar"
        onSubmit={(e) => {
          e.preventDefault();
          load();
        }}
      >
        <input
          type="text"
          placeholder="Filter by subject"
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
        />
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">all statuses</option>
          <option value="ok">ok</option>
          <option value="denied">denied</option>
          <option value="no_identity_mapping">no mapping</option>
          <option value="error">error</option>
        </select>
        <button type="submit">Filter</button>
      </form>

      {error && <p className="form-error">{error}</p>}
      {loading ? (
        <p className="muted">Loading…</p>
      ) : entries.length === 0 ? (
        <p className="muted">No audit entries match this filter.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Tool</th>
                <th>Subject</th>
                <th>Env</th>
                <th>Status</th>
                <th>Latency</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <Fragment key={entry.id}>
                  <tr
                    className="clickable-row"
                    onClick={() => setExpandedId(expandedId === entry.id ? null : entry.id)}
                  >
                    <td className="muted">{new Date(entry.occurred_at).toLocaleString()}</td>
                    <td>
                      <code>{entry.tool_name}</code>
                    </td>
                    <td>{entry.subject}</td>
                    <td>
                      <span className="pill">{entry.environment}</span>
                    </td>
                    <td>
                      <StatusBadge status={entry.status} />
                    </td>
                    <td className="muted">{entry.latency_ms.toFixed(2)} ms</td>
                  </tr>
                  {expandedId === entry.id && (
                    <tr className="detail-row">
                      <td colSpan={6}>
                        <div className="detail-grid">
                          <div>
                            <strong>Correlation ID</strong>
                            <div>{entry.correlation_id}</div>
                          </div>
                          <div>
                            <strong>Effective Org IDs</strong>
                            <div>{entry.effective_org_ids?.join(", ") || "—"}</div>
                          </div>
                          {entry.error_message && (
                            <div className="detail-error">
                              <strong>Error</strong>
                              <div>{entry.error_message}</div>
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
