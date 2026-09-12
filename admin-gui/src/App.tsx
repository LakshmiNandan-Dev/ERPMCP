import { useEffect, useState } from "react";
import "./App.css";
import { getBranding } from "./api/client";
import { AdminSubjectBar } from "./components/AdminSubjectBar";
import { AuditLog } from "./components/AuditLog";
import { BrandingSettings } from "./components/BrandingSettings";
import { EntraSettings } from "./components/EntraSettings";
import { IdentityMappings } from "./components/IdentityMappings";
import type { Branding } from "./types";

type Tab = "mappings" | "audit" | "settings" | "branding";

const DEFAULT_TITLE = "EBSMCP Admin";

function App() {
  const [tab, setTab] = useState<Tab>("mappings");
  const [branding, setBranding] = useState<Branding | null>(null);

  // Unauthenticated read, deliberately: this renders above the sign-in form,
  // so a customer's own name has to appear on the login screen and not only
  // after they get in. A failure here is silent — falling back to the product
  // default is strictly better than blocking the console on a cosmetic call.
  useEffect(() => {
    getBranding()
      .then(setBranding)
      .catch(() => setBranding(null));
  }, []);

  // site_name is what this console calls itself; company_name is who owns
  // the deployment. Falling through in that order keeps an existing
  // company-name-only configuration rendering exactly as it did before
  // site_name existed.
  const title = branding?.site_name?.trim() || branding?.company_name?.trim() || DEFAULT_TITLE;
  // Only worth showing as a second line when it is not already the title.
  const subtitle =
    branding?.site_name?.trim() && branding?.company_name?.trim() ? branding.company_name.trim() : null;

  // The tab title was hardcoded in index.html and never updated, so a branded
  // deployment still read "EBSMCP Admin" in the browser tab and in bookmarks.
  useEffect(() => {
    document.title = title;
  }, [title]);

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-brand">
          {branding?.logo_data_uri && <img className="app-logo" src={branding.logo_data_uri} alt="" />}
          <div className="app-brand-text">
            <h1>{title}</h1>
            {subtitle && <span className="app-brand-org">{subtitle}</span>}
          </div>
        </div>
        <AdminSubjectBar />
      </header>

      <nav className="tab-bar">
        <button className={tab === "mappings" ? "tab active" : "tab"} onClick={() => setTab("mappings")}>
          Identity mappings
        </button>
        <button className={tab === "audit" ? "tab active" : "tab"} onClick={() => setTab("audit")}>
          Audit log
        </button>
        <button className={tab === "settings" ? "tab active" : "tab"} onClick={() => setTab("settings")}>
          Settings
        </button>
        <button className={tab === "branding" ? "tab active" : "tab"} onClick={() => setTab("branding")}>
          Branding
        </button>
      </nav>

      <main>
        {tab === "mappings" && <IdentityMappings />}
        {tab === "audit" && <AuditLog />}
        {tab === "settings" && <EntraSettings />}
        {tab === "branding" && <BrandingSettings />}
      </main>
    </div>
  );
}

export default App;
