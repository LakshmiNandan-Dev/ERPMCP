// Real local admin sign-in for admin-gui itself — deliberately username +
// password against management-api's own admin_accounts table, not
// Entra/SSO (see management-api's app.config.Settings.admin_session_secret
// comment: admin access to this tool is meant to stay independent of
// whichever customer Entra tenant a deployment happens to serve). A
// different concern entirely from entra_registrations (edited on the
// Settings tab), which is the tenant mcp-server trusts for *end users*.
//
// Mirrors management-api's own unarmed-until-configured contract: false
// keeps today's free-text "Acting as" stub; true switches to a real
// login form, calling POST /auth/login (which itself 503s server-side
// until ADMIN_SESSION_SECRET and at least one admin_accounts row exist).
export const LOCAL_ADMIN_AUTH_ENABLED = import.meta.env.VITE_LOCAL_ADMIN_AUTH_ENABLED === "true";
