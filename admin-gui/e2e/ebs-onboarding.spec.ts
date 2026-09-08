import { expect, test } from "@playwright/test";

// Exercises the corrected onboarding flow: Entra identity and EBS account
// are different identifiers that must be explicitly mapped, and Org IDs
// come from what EBS actually grants JDOE — never free-typed. Relies on
// MockEBSLookupConnector's canned Vision data (see
// management-api/app/ebs/lookup.py): "Payables Manager" resolves to a
// single org via MO: Operating Unit, "General Ledger Multi-Org Reporting"
// resolves to three via MO: Security Profile.
const subject = () => `ebs-onboard.${Date.now()}@corp.com`;

test("look up an EBS user, select a responsibility, narrow the resolved orgs", async ({ page }) => {
  const entraSubject = subject();

  await page.goto("/");
  await page.fill("#admin-subject", "e2e.onboard@corp.com");

  await page.fill('input[placeholder="j.doe@corp.com"]', entraSubject);
  await page.fill('input[placeholder="JDOE"]', "JDOE");
  await page.click('button:has-text("Look up user")');

  await page.locator("label", { hasText: "Responsibility" }).locator("select").selectOption({
    label: "General Ledger Multi-Org Reporting (finance)",
  });
  await expect(page.locator(".org-checkbox-row")).toHaveCount(3);
  await expect(page.locator("text=Domain:")).toBeVisible();

  // Narrow: drop Vision Italy before submitting.
  await page.locator(".org-checkbox-row", { hasText: "Vision Italy" }).locator('input[type="checkbox"]').uncheck();
  await page.click('button:has-text("Create mapping")');

  const row = page.locator("tr", { hasText: entraSubject });
  await expect(row).toBeVisible();
  await expect(row.locator("code")).toHaveText("JDOE");
  await expect(row.locator("td").nth(4)).toHaveText("finance");
  await expect(row.locator(".pill-org")).toHaveText(["204", "207"]); // narrowed set, not all three
});

test("a single-org responsibility resolves via MO: Operating Unit, not Security Profile", async ({ page }) => {
  const entraSubject = subject();

  await page.goto("/");
  await page.fill("#admin-subject", "e2e.onboard@corp.com");
  await page.fill('input[placeholder="j.doe@corp.com"]', entraSubject);
  await page.fill('input[placeholder="JDOE"]', "JDOE");
  await page.click('button:has-text("Look up user")');

  await page.locator("label", { hasText: "Responsibility" }).locator("select").selectOption({ label: "Payables Manager (finance)" });
  await expect(page.locator(".org-checkbox-row")).toHaveCount(1);
  await expect(page.locator("text=/MO: Operating Unit/")).toBeVisible();

  await page.click('button:has-text("Create mapping")');
  const row = page.locator("tr", { hasText: entraSubject });
  await expect(row.locator(".pill-org")).toHaveText(["204"]);
});

test("an unknown EBS username surfaces the lookup error, not a silent empty form", async ({ page }) => {
  await page.goto("/");
  await page.fill("#admin-subject", "e2e.onboard@corp.com");
  await page.fill('input[placeholder="j.doe@corp.com"]', subject());
  await page.fill('input[placeholder="JDOE"]', "NOBODY");
  await page.click('button:has-text("Look up user")');
  await expect(page.locator(".form-error")).toContainText("No active responsibilities found");
});
