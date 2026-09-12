import { useCallback, useEffect, useState } from "react";
import { ApiError, getBranding, putBranding } from "../api/client";
import { useAdminIdentity } from "../auth/AdminIdentity";
import type { Branding } from "../types";

// ~256 KB of base64 is what management-api accepts (see schemas/branding.py).
// Checked here too so an oversized file is refused at the file picker with a
// useful message, rather than after an upload round trip that 422s.
const MAX_LOGO_CHARS = 256_000;
const ALLOWED_TYPES = ["image/png", "image/jpeg", "image/svg+xml", "image/webp"];

function formatDate(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

export function BrandingSettings() {
  const { subject: adminSubject } = useAdminIdentity();
  const [current, setCurrent] = useState<Branding | null>(null);
  const [siteName, setSiteName] = useState("");
  const [companyName, setCompanyName] = useState("");
  const [logo, setLogo] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setSaved(false);
    try {
      const branding = await getBranding();
      setCurrent(branding);
      setSiteName(branding.site_name ?? "");
      setCompanyName(branding.company_name ?? "");
      setLogo(branding.logo_data_uri);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load branding.");
    } finally {
      setLoading(false);
    }
  }, []);

  // No adminSubject guard on load, unlike the other settings pages: this GET
  // is unauthenticated on purpose, because the header reads it before sign-in.
  useEffect(() => {
    load();
  }, [load]);

  // The API takes an inline data URI and there is no upload endpoint or
  // writable volume anywhere in this stack, so the conversion happens here.
  const handleFile = (file: File | null) => {
    setError(null);
    setSaved(false);
    if (!file) {
      setLogo(null);
      return;
    }
    if (!ALLOWED_TYPES.includes(file.type)) {
      setError(`${file.type || "That file"} is not a supported image — use PNG, JPEG, SVG or WebP.`);
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => setError("Could not read that file.");
    reader.onload = () => {
      const result = String(reader.result);
      if (result.length > MAX_LOGO_CHARS) {
        setError(
          `That image is too large once encoded (${Math.round(result.length / 1024)} KB of ` +
            `${Math.round(MAX_LOGO_CHARS / 1024)} KB allowed). Scale it down — a header logo ` +
            "rarely needs to be more than a few hundred pixels wide.",
        );
        return;
      }
      setLogo(result);
    };
    reader.readAsDataURL(file);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSaved(false);

    if (!adminSubject) {
      setError('Sign in (or set "Acting as" above) before saving — branding changes are attributed.');
      return;
    }

    setSaving(true);
    try {
      const result = await putBranding({
        site_name: siteName.trim() || null,
        company_name: companyName.trim() || null,
        logo_data_uri: logo,
      });
      setCurrent(result);
      setSaved(true);
      // The header reads branding independently, so it will not notice this
      // save on its own — tell the user rather than leaving them wondering
      // why the title above still says the old name.
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save branding.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page">
      <div className="section-header">
        <h2>Branding</h2>
      </div>

      <p className="muted">
        The company name and logo shown in this console's header. Stored in the database, not baked into the
        image — changing either takes effect on the next page load, with no rebuild or redeploy.
      </p>

      {loading ? (
        <p className="muted">Loading…</p>
      ) : (
        <form className="new-mapping-form" onSubmit={handleSubmit}>
          {current?.updated_by ? (
            <p className="muted">
              Last updated by {current.updated_by} on {formatDate(current.updated_at)}.
            </p>
          ) : (
            <p className="muted">Not configured yet — the console is showing its default name.</p>
          )}

          <div className="form-row">
            <label>
              Site name
              <input
                type="text"
                maxLength={120}
                placeholder="EBSMCP Admin"
                value={siteName}
                onChange={(e) => setSiteName(e.target.value)}
              />
              <small className="muted">
                What this console is called — shown as the header title and the browser tab.
              </small>
            </label>
          </div>

          <div className="form-row">
            <label>
              Company name
              <input
                type="text"
                maxLength={120}
                placeholder="Your organisation"
                value={companyName}
                onChange={(e) => setCompanyName(e.target.value)}
              />
              <small className="muted">
                The organisation that owns this deployment. Used as the title when no site name is set.
              </small>
            </label>
          </div>

          <div className="form-row">
            <label>
              Logo
              <input
                type="file"
                accept={ALLOWED_TYPES.join(",")}
                onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
              />
            </label>
          </div>

          {logo && (
            <div className="branding-preview">
              <img src={logo} alt="Logo preview" />
              <button type="button" className="lookup-btn" onClick={() => setLogo(null)}>
                Remove logo
              </button>
            </div>
          )}

          {error && <p className="form-error">{error}</p>}
          {saved && <p className="muted">Saved — reload the page to see it in the header.</p>}

          <button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save branding"}
          </button>
        </form>
      )}
    </div>
  );
}
