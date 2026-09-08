import { createContext, useContext, useState, type ReactNode } from "react";
import { login as loginRequest, setAuthHeaders } from "../api/client";
import { LOCAL_ADMIN_AUTH_ENABLED } from "./config";

interface AdminIdentity {
  // Who's acting, for display and for the pre-flight "you must be signed
  // in" checks components already made — null until known (real mode,
  // before sign-in) or while the user hasn't typed one yet (stub mode).
  subject: string | null;
  usesRealAuth: boolean;
  // Present only in real-auth mode.
  signIn?: (username: string, password: string) => Promise<void>;
  signOut?: () => void;
  // Present only in stub mode — the free-text "Acting as" field.
  setStubSubject?: (value: string) => void;
}

const AdminIdentityContext = createContext<AdminIdentity>({ subject: null, usesRealAuth: false });

export function useAdminIdentity(): AdminIdentity {
  return useContext(AdminIdentityContext);
}

function StubAdminIdentityProvider({ children }: { children: ReactNode }) {
  const [subject, setSubject] = useState("");

  // Deliberately not a useEffect keyed on `subject`: an effect here would
  // run in a separate pass from the one that re-renders every consumer
  // reading this context, with no guaranteed ordering between the two —
  // caught by an actual Playwright run, where a consumer's own
  // adminSubject-triggered fetch fired before this effect had attached
  // the header, producing one spurious "header is required" error before
  // the real result appeared. Setting the header synchronously, in the
  // same event handler that changes `subject`, closes that gap: nothing
  // can observe a new subject value before the header matching it exists.
  const setStubSubject = (value: string) => {
    setSubject(value);
    setAuthHeaders(value ? { "X-Admin-Subject": value } : {});
  };

  return (
    <AdminIdentityContext.Provider value={{ subject: subject || null, usesRealAuth: false, setStubSubject }}>
      {children}
    </AdminIdentityContext.Provider>
  );
}

const SESSION_STORAGE_KEY = "ebsmcp_admin_session";

function LocalAccountIdentityProvider({ children }: { children: ReactNode }) {
  // Restored synchronously from sessionStorage on first render, not in an
  // effect — same "the header must exist before anything can observe a
  // non-null subject" reasoning as the stub provider above. A stale or
  // tampered token still gets caught: the very next request 401s and
  // signOut() clears it, same as any other expired session.
  const [subject, setSubject] = useState<string | null>(() => {
    const stored = readStoredSession();
    if (stored) setAuthHeaders({ Authorization: `Bearer ${stored.token}` });
    return stored?.subject ?? null;
  });

  const signIn = async (username: string, password: string) => {
    const result = await loginRequest(username, password);
    sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({ token: result.access_token, subject: result.subject }));
    setAuthHeaders({ Authorization: `Bearer ${result.access_token}` });
    setSubject(result.subject);
  };

  const signOut = () => {
    sessionStorage.removeItem(SESSION_STORAGE_KEY);
    setAuthHeaders({});
    setSubject(null);
  };

  return (
    <AdminIdentityContext.Provider value={{ subject, usesRealAuth: true, signIn, signOut }}>
      {children}
    </AdminIdentityContext.Provider>
  );
}

function readStoredSession(): { token: string; subject: string } | null {
  try {
    const raw = sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (typeof parsed?.token === "string" && typeof parsed?.subject === "string") return parsed;
    return null;
  } catch {
    return null;
  }
}

export function AdminIdentityProvider({ children }: { children: ReactNode }) {
  if (LOCAL_ADMIN_AUTH_ENABLED) {
    return <LocalAccountIdentityProvider>{children}</LocalAccountIdentityProvider>;
  }
  return <StubAdminIdentityProvider>{children}</StubAdminIdentityProvider>;
}
