import { useState } from "react";
import "./App.css";
import { AdminSubjectBar } from "./components/AdminSubjectBar";
import { AuditLog } from "./components/AuditLog";
import { EntraSettings } from "./components/EntraSettings";
import { IdentityMappings } from "./components/IdentityMappings";

type Tab = "mappings" | "audit" | "settings";

function App() {
  const [tab, setTab] = useState<Tab>("mappings");

  return (
    <div className="app-shell">
      <header className="app-header">
        <h1>EBSMCP Admin</h1>
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
      </nav>

      <main>
        {tab === "mappings" && <IdentityMappings />}
        {tab === "audit" && <AuditLog />}
        {tab === "settings" && <EntraSettings />}
      </main>
    </div>
  );
}

export default App;
