import { expect, test } from "@playwright/test";

// Uses a randomized subject per run so this is safe to re-run against a
// persistent database without colliding with a previous run's open-ended
// mapping (the same uniqueness constraint verified at the schema layer).
const subject = () => `e2e.${Date.now()}@corp.com`;

test("create a Fusion mapping via manual entry, see it listed, then close it", async ({ page }) => {
  // Fusion has no equivalent of the EBS lookup yet (see NewMappingForm's
  // fallback branch) — this is the one remaining manual-entry path, and
  // it's worth its own coverage precisely because it's the exception now,
  // not the default the way both org-scope fields used to be for EBS too.
  const entraSubject = subject();

  await page.goto("/");
  await expect(page.locator("h1")).toHaveText("EBSMCP Admin");

  await page.fill("#admin-subject", "e2e.admin@corp.com");
  await page.locator("label", { hasText: "Target system" }).locator("select").selectOption("fusion");

  await page.fill('input[placeholder="j.doe@corp.com"]', entraSubject);
  await page.fill('input[placeholder="jdoe"]', "jdoe");
  await page.fill('input[placeholder="AP Invoice Reviewer"]', "AP Invoice Reviewer");
  await page.locator("label", { hasText: "Domain" }).locator("select").selectOption("finance");
  await page.fill('input[placeholder="BU ID"]', "300000001234567");
  await page.click('button:has-text("Create mapping")');

  const row = page.locator("tr", { hasText: entraSubject });
  await expect(row).toBeVisible();
  await expect(row.locator("code")).toHaveText("jdoe");
  await expect(row.locator("td").nth(4)).toHaveText("finance");
  await expect(row.locator(".pill-org")).toHaveText("300000001234567");

  await row.locator('button:has-text("Close")').click();
  await expect(row).toHaveCount(0);

  await page.getByLabel("include closed").check();
  await expect(page.locator("tr", { hasText: entraSubject })).toBeVisible();
});

test("audit log tab renders and is filterable", async ({ page }) => {
  await page.goto("/");
  await page.click('button:has-text("Audit log")');
  await expect(page.locator(".audit-note")).toContainText("Read-only");

  await page.fill('input[placeholder="Filter by subject"]', "no-such-subject@corp.com");
  await page.click('button:has-text("Filter")');
  await expect(page.locator("text=No audit entries match this filter.")).toBeVisible();
});
