import { defineConfig } from "@playwright/test";

// Assumes management-api is already running and reachable at
// VITE_API_BASE_URL (default http://localhost:8010), against a real
// migrated database — same assumption the manual verification for this
// project has used throughout. This suite does not spin up the backend
// itself; it's a frontend E2E check, not a full-stack integration harness.
export default defineConfig({
  testDir: "./e2e",
  webServer: {
    command: "npm run dev -- --port 5180",
    url: "http://localhost:5180",
    reuseExistingServer: true,
  },
  use: {
    baseURL: "http://localhost:5180",
  },
});
