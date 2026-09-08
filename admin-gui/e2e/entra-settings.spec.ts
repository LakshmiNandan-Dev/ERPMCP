import { expect, test } from "@playwright/test";

// "test" is used as the target environment here specifically because it's
// unlikely to collide with a real "dev"/"uat"/"prod" registration someone
// is actually relying on while this suite runs against a shared database.
test("switching environments never shows a stale in-flight fetch's data", async ({ page }) => {
  await page.goto("/");
  await page.fill("#admin-subject", "e2e.settings@corp.com");
  await page.click('button:has-text("Settings")');

  // Rapid-fire environment switches are exactly what triggered the race
  // condition this test exists to catch — whichever fetch resolved last
  // used to win the UI state regardless of which environment was actually
  // selected. See EntraSettings.tsx's latestRequestId guard.
  const envSelect = page.locator(".env-picker select");
  await envSelect.selectOption("dev");
  await envSelect.selectOption("uat");
  await envSelect.selectOption("test");

  await expect(envSelect).toHaveValue("test");
  // Whatever renders must be test's own state, not a leftover from dev/uat.
  await expect(page.locator(".callout-warning")).toContainText("trusts for test");
});

test("create then update an Entra registration, values persist across reload", async ({ page }) => {
  const tenantId = `e2e-tenant-${Date.now()}`;

  await page.goto("/");
  await page.fill("#admin-subject", "e2e.settings@corp.com");
  await page.click('button:has-text("Settings")');
  await page.selectOption(".env-picker select", "uat");

  await page.fill('input[placeholder="00000000-0000-0000-0000-000000000000"]', tenantId);
  await page.fill('input[placeholder="11111111-1111-1111-1111-111111111111"]', "e2e-audience");
  await page.fill('input[placeholder="https://ebsmcp.corp.com/"]', "https://uat.ebsmcp.corp.com/");
  // Against a persistent database (not a fresh one per run), "uat" may
  // already be configured from an earlier run of this same test — the
  // save button correctly reads "Update" rather than "Create" then. The
  // save behavior under test is the same either way; only the button's
  // label depends on prior state, so match either rather than assume this
  // is the first time this test has ever run against this database.
  await page.click('button:has-text("configuration")');

  await expect(page.locator("text=Saved. Restart mcp-server for this to take effect.")).toBeVisible();

  await page.reload();
  // A reload drops the stub "Acting as" state (it's plain in-memory React
  // state, not persisted) — the GET below needs it refilled the same as
  // any other admin-gui-facing read now does.
  await page.fill("#admin-subject", "e2e.settings@corp.com");
  await page.click('button:has-text("Settings")');
  await page.selectOption(".env-picker select", "uat");
  await expect(page.locator('input[placeholder="00000000-0000-0000-0000-000000000000"]')).toHaveValue(tenantId);
  await expect(page.locator("text=Currently configured")).toBeVisible();
});
