import { Fragment, useCallback, useEffect, useState } from "react";
import { ApiError, listAuditLog } from "../api/client";
import type { AuditLogEntry, AuditStatus, Environment } from "../types";

const STATUS_LABEL: Record<AuditStatus, string> = {
  ok: "ok",
  denied: "denied",
  no_identity_mapping: "no mapping",
  error: "error",
};

const ENVIRONMENTS: Environment[] = ["dev", "test", "uat", "prod"];

function StatusBadge({ status }: { status: AuditStatus }) {
  return <span className={`status-badge status-${status}`}>{STATUS_LABEL[status]}</span>;
}

/** What the form currently shows. Kept separate from the filters actually
 *  applied, so typing in a text box doesn't fire a request per keystroke —
 *  with seven controls that would be a request storm against an audit table. */
interface Draft {
  subject: string;
  environment: string;
  instance: string;
  toolName: string;
  status: string;
  from: string;
  to: string;
}

const EMPTY: Draft = {
  subject: "",
  environment: "",
  instance: "",
  toolName: "",
  status: "",
  from: "",
  to: "",
};

/** A date picked in the console means a whole local day. Translate it to an
 *  absolute instant so the server's >= / <= mean what the operator expects —
 *  a bare "2026-09-10" is midnight, which would otherwise exclude that entire
 *  day from a "to" filter. */
function startOfDay(date: string): string | undefined {
  return date ? new Date(`${date}T00:00:00`).toISOString() : undefined;
}

function endOfDay(date: string): string | undefined {
  return date ? new Date(`${date}T23:59:59.999`).toISOString() : undefined;
}

export function AuditLog() {
  const [entries, setEntries] = useState<AuditLogEntry[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [applied, setApplied] = useState<Draft>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setEntries(
        await listAuditLog({
          subject: applied.subject || undefined,
          environment: applied.environment || undefined,
          instance: applied.instance || undefined,
          toolName: applied.toolName || undefined,
          status: applied.status || undefined,
          since: startOfDay(applied.from),
          until: endOfDay(applied.to),
        }),
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load the audit log.");
    } finally {
      setLoading(false);
    }
  }, [applied]);

  useEffect(() => {
    load();
  }, [load]);

  const isFiltered = Object.values(applied).some(Boolean);

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
          setApplied(draft);
        }}
      >
        <label className="filter-field">
          <span>User</span>
          <input
            type="text"
            placeholder="name or partial email"
            value={draft.subject}
            onChange={(e) => set("subject", e.target.value)}
          />
        </label>

        <label className="filter-field">
          <span>Environment</span>
          <select value={draft.environment} onChange={(e) => set("environment", e.target.value)}>
            <option value="">all</option>
            {ENVIRONMENTS.map((env) => (
              <option key={env} value={env}>
                {env}
              </option>
            ))}
          </select>
        </label>

        <label className="filter-field">
          <span>Instance</span>
          <input
            type="text"
            placeholder="e.g. PROD"
            value={draft.instance}
            onChange={(e) => set("instance", e.target.value)}
          />
        </label>

        <label className="filter-field">
          <span>Tool</span>
          <input
            type="text"
            placeholder="e.g. instance_status"
            value={draft.toolName}
            onChange={(e) => set("toolName", e.target.value)}
          />
        </label>

        <label className="filter-field">
          <span>Status</span>
          <select value={draft.status} onChange={(e) => set("status", e.target.value)}>
            <option value="">all statuses</option>
            <option value="ok">ok</option>
            <option value="denied">denied</option>
            <option value="no_identity_mapping">no mapping</option>
            <option value="error">error</option>
          </select>
        </label>

        <label className="filter-field">
          <span>From</span>
          <input type="date" value={draft.from} onChange={(e) => set("from", e.target.value)} />
        </label>

        <label className="filter-field">
          <span>To</span>
          <input type="date" value={draft.to} onChange={(e) => set("to", e.target.value)} />
        </label>

        <button type="submit">Filter</button>
        {isFiltered && (
          <button
            type="button"
            onClick={() => {
              setDraft(EMPTY);
              setApplied(EMPTY);
            }}
          >
            Clear
          </button>
        )}
      </form>

      {error && <p className="form-error">{error}</p>}
      {loading ? (
        <p className="muted">Loading…</p>
      ) : entries.length === 0 ? (
        <p className="muted">
          {isFiltered ? "No audit entries match this filter." : "No audit entries yet."}
        </p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Tool</th>
                <th>Subject</th>
                <th>Env</th>
                <th>Instance</th>
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
                    {/* No instance is a real answer for tools that touch none
                        (list_ebs_instances), not missing data. */}
                    <td>{entry.instance ? <span className="pill">{entry.instance}</span> : <span className="muted">—</span>}</td>
                    <td>
                      <StatusBadge status={entry.status} />
                    </td>
                    <td className="muted">{entry.latency_ms.toFixed(2)} ms</td>
                  </tr>
                  {expandedId === entry.id && (
                    <tr className="detail-row">
                      <td colSpan={7}>
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
