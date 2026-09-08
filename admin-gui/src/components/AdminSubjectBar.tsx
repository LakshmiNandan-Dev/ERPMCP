import { useState } from "react";
import { ApiError } from "../api/client";
import { useAdminIdentity } from "../auth/AdminIdentity";

// Two modes, switching automatically based on VITE_LOCAL_ADMIN_AUTH_ENABLED
// (see src/auth/config.ts) — same activation contract as management-api's
// own admin_session_secret: nothing else in the app needs to change when
// real local accounts are set up, because every caller of
// useAdminIdentity() only ever sees a subject string either way.
export function AdminSubjectBar() {
  const identity = useAdminIdentity();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [signingIn, setSigningIn] = useState(false);

  if (identity.usesRealAuth) {
    if (identity.subject) {
      return (
        <div className="admin-subject-bar">
          <span>Signed in as {identity.subject}</span>
          <button type="button" className="lookup-btn" onClick={identity.signOut}>
            Sign out
          </button>
        </div>
      );
    }

    const handleLogin = async (e: React.FormEvent) => {
      e.preventDefault();
      setError(null);
      setSigningIn(true);
      try {
        await identity.signIn?.(username, password);
        setPassword("");
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Sign-in failed.");
      } finally {
        setSigningIn(false);
      }
    };

    return (
      <form className="admin-subject-bar" onSubmit={handleLogin}>
        <input
          type="text"
          placeholder="Username"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <input
          type="password"
          placeholder="Password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button type="submit" className="lookup-btn" disabled={signingIn}>
          {signingIn ? "Signing in…" : "Sign in"}
        </button>
        {error && <span className="admin-subject-note form-error">{error}</span>}
      </form>
    );
  }

  return (
    <div className="admin-subject-bar">
      <label htmlFor="admin-subject">Acting as</label>
      <input
        id="admin-subject"
        type="email"
        placeholder="admin@corp.com"
        value={identity.subject ?? ""}
        onChange={(e) => identity.setStubSubject?.(e.target.value)}
      />
      <span className="admin-subject-note">stand-in for real admin sign-in</span>
    </div>
  );
}
