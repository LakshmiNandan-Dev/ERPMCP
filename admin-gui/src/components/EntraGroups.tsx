import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  addEntraGroup,
  closeEntraGroup,
  getEntraConfig,
  getGroupDomains,
  listEntraGroups,
  putEntraConfig,
} from "../api/client";
import { useAdminIdentity } from "../auth/AdminIdentity";
import type { EntraGroup, EntraRegistration, Environment, GroupChannel } from "../types";

const CHANNEL_LABELS: Record<GroupChannel, string> = {
  chat_agent: "Chat agent",
  mcp_direct: "MCP client (Entra token)",
  any: "Both",
};

const EMPTY_FORM = { group_object_id: "", group_name: "", channel: "chat_agent" as GroupChannel };

function formatDate(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

export function EntraGroups() {
  const { subject: adminSubject } = useAdminIdentity();
  const [environment, setEnvironment] = useState<Environment>("prod");
  const [config, setConfig] = useState<EntraRegistration | null>(null);
  const [groups, setGroups] = useState<EntraGroup[]>([]);
  const [domains, setDomains] = useState<string[]>([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [allDomains, setAllDomains] = useState(true);
  const [picked, setPicked] = useState<string[]>([]);
  const [claim, setClaim] = useState("groups");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Same race guard as EntraSettings: a slow response for the previous
  // environment must not overwrite the one now selected.
  const latestRequestId = useRef(0);

  const load = useCallback(async () => {
    const requestId = ++latestRequestId.current;
    setLoading(true);
    setError(null);
    try {
      const [cfg, list] = await Promise.all([getEntraConfig(environment), listEntraGroups(environment)]);
      if (latestRequestId.current !== requestId) return;
      setConfig(cfg);
      setClaim(cfg?.groups_claim ?? "groups");
      setGroups(list);
    } catch (err) {
      if (latestRequestId.current !== requestId) return;
      setError(err instanceof ApiError ? err.message : "Failed to load group access.");
    } finally {
      if (latestRequestId.current === requestId) setLoading(false);
    }
  }, [environment]);

  useEffect(() => {
    if (!adminSubject) return;
    load();
  }, [load, adminSubject]);

  useEffect(() => {
    if (!adminSubject) return;
    getGroupDomains()
      .then(setDomains)
      .catch(() => setDomains([]));
  }, [adminSubject]);

  const run = async (action: () => Promise<void>, failure: string) => {
    if (!adminSubject) {
      setError('Set "Acting as" above first — group access changes are attributed.');
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : failure);
    } finally {
      setBusy(false);
    }
  };

  // The tenant fields are echoed back unchanged: this page only edits the
  // enforcement switch and the groups claim, and the PUT replaces the rest.
  const saveEnforcement = (enforce: boolean, groupsClaim: string) =>
    run(async () => {
      if (!config) return;
      const saved = await putEntraConfig(environment, {
        tenant_id: config.tenant_id,
        audience: config.audience,
        subject_claim: config.subject_claim,
        resource_server_url: config.resource_server_url,
        enforce_group_access: enforce,
        groups_claim: groupsClaim.trim() || "groups",
      });
      setConfig(saved);
      setNotice(
        saved.enforce_group_access
          ? "Group access is now enforced. This applies on the next request."
          : "Group access is no longer enforced.",
      );
    }, "Failed to save.");

  const handleAdd = (e: React.FormEvent) => {
    e.preventDefault();
    const allowed = allDomains ? ["*"] : picked;
    if (allowed.length === 0) {
      setError("Choose at least one domain, or all domains.");
      return;
    }
    run(async () => {
      await addEntraGroup(environment, { ...form, allowed_domains: allowed });
      setForm(EMPTY_FORM);
      setPicked([]);
      setAllDomains(true);
      setNotice("Group added. It applies on the next request.");
      await load();
    }, "Failed to add the group.");
  };

  const handleClose = (g: EntraGroup) => {
    if (!window.confirm(`Remove access for "${g.group_name}"? Its members lose access on their next request.`)) return;
    run(async () => {
      await closeEntraGroup(environment, g.id);
      setNotice(`Closed the grant for "${g.group_name}".`);
      await load();
    }, "Failed to close the grant.");
  };

  const enforced = config?.enforce_group_access ?? false;

  return (
    <div className="page">
      <div className="section-header">
        <h2>Group access</h2>
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
        Restrict who may use the tools to members of specific Microsoft Entra ID (Active Directory) groups. Unlike the
        tenant settings, a change here applies on the <strong>next request</strong> — closing a group revokes its
        members immediately. A group can only narrow what a person's identity mapping already allows; it never adds
        access. Groups are identified by their <strong>object ID</strong> (a GUID), not their name.
      </div>

      {loading ? (
        <p className="muted">Loading…</p>
      ) : !config ? (
        <p className="muted">
          No Entra configuration exists for {environment} yet. Create it under Settings first — group access builds on
          the tenant it defines.
        </p>
      ) : (
        <>
          <div className="new-mapping-form">
            <p>
              Group enforcement is <strong>{enforced ? "ON" : "OFF"}</strong> for {environment}.{" "}
              {enforced
                ? "Anyone outside the groups below is denied, and so is anyone whose groups cannot be determined."
                : "Until it is turned on, no group check is made."}
            </p>
            <div className="form-row">
              <label>
                Token claim carrying group IDs
                <input type="text" value={claim} onChange={(e) => setClaim(e.target.value)} />
              </label>
            </div>
            <button
              type="button"
              className="submit-btn"
              disabled={busy || (!enforced && groups.length === 0)}
              title={!enforced && groups.length === 0 ? "Add a group first — otherwise everyone would be denied." : ""}
              onClick={() => saveEnforcement(!enforced, claim)}
            >
              {enforced ? "Turn enforcement off" : "Turn enforcement on"}
            </button>{" "}
            {enforced && (
              <button type="button" className="submit-btn" disabled={busy} onClick={() => saveEnforcement(true, claim)}>
                Save claim name
              </button>
            )}
          </div>

          <div className="table-scroll">
            <table className="mappings-table">
              <thead>
                <tr>
                  <th>Group</th>
                  <th>Object ID</th>
                  <th>Channel</th>
                  <th>Domains</th>
                  <th>Added</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {groups.length === 0 && (
                  <tr>
                    <td colSpan={6} className="muted">
                      No groups granted for {environment}.
                    </td>
                  </tr>
                )}
                {groups.map((g) => (
                  <tr key={g.id}>
                    <td>{g.group_name}</td>
                    <td>
                      <code>{g.group_object_id}</code>
                    </td>
                    <td>{CHANNEL_LABELS[g.channel]}</td>
                    <td>{g.allowed_domains.includes("*") ? "All domains" : g.allowed_domains.join(", ")}</td>
                    <td>
                      {g.created_by}
                      <br />
                      <span className="muted">{formatDate(g.created_at)}</span>
                    </td>
                    <td>
                      <button type="button" disabled={busy} onClick={() => handleClose(g)}>
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <form className="new-mapping-form" onSubmit={handleAdd}>
            <h3>Grant a group</h3>
            <div className="form-row">
              <label>
                Group object ID
                <input
                  type="text"
                  required
                  placeholder="0b3f1c2e-1111-2222-3333-444455556666"
                  value={form.group_object_id}
                  onChange={(e) => setForm((f) => ({ ...f, group_object_id: e.target.value }))}
                />
              </label>
              <label>
                Name (for this console only)
                <input
                  type="text"
                  required
                  placeholder="EBS Finance Users"
                  value={form.group_name}
                  onChange={(e) => setForm((f) => ({ ...f, group_name: e.target.value }))}
                />
              </label>
              <label>
                Channel
                <select
                  value={form.channel}
                  onChange={(e) => setForm((f) => ({ ...f, channel: e.target.value as GroupChannel }))}
                >
                  {(Object.keys(CHANNEL_LABELS) as GroupChannel[]).map((c) => (
                    <option key={c} value={c}>
                      {CHANNEL_LABELS[c]}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <fieldset>
              <legend>Domains members may use</legend>
              <label>
                <input type="checkbox" checked={allDomains} onChange={(e) => setAllDomains(e.target.checked)} /> All
                domains
              </label>
              {!allDomains &&
                domains.map((d) => (
                  <label key={d} style={{ marginLeft: "1rem" }}>
                    <input
                      type="checkbox"
                      checked={picked.includes(d)}
                      onChange={(e) =>
                        setPicked((p) => (e.target.checked ? [...p, d] : p.filter((x) => x !== d)))
                      }
                    />{" "}
                    {d === "dba" ? "dba (database tools)" : d}
                  </label>
                ))}
            </fieldset>
            <button type="submit" className="submit-btn" disabled={busy}>
              {busy ? "Saving…" : "Grant access"}
            </button>
          </form>
        </>
      )}

      {error && <p className="form-error">{error}</p>}
      {notice && <p className="form-success">{notice}</p>}
    </div>
  );
}
