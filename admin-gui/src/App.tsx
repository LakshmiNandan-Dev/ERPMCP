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

  const title = branding?.company_name?.trim() || DEFAULT_TITLE;

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-brand">
          {branding?.logo_data_uri && <img className="app-logo" src={branding.logo_data_uri} alt="" />}
          <h1>{title}</h1>
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
