import { expect, test } from "@playwright/test";

// ebs_dba is a persona, not a backend: no FND_USER lookup applies, and
// access is all-or-nothing (the full DBA toolset) — no per-category
// selection. See NewMappingForm's ebs_dba branch and management-api's
// rejection of any org_scope on an ebs_dba mapping.
const subject = () => `dba-onboard.${Date.now()}@corp.com`;

test("DBA mapping skips the EBS lookup entirely and needs no scope selection", async ({ page }) => {
  const entraSubject = subject();

  await page.goto("/");
  await page.fill("#admin-subject", "e2e.dba@corp.com");

  await page.fill('input[placeholder="j.doe@corp.com"]', entraSubject);
  await page.locator("label", { hasText: "Target system" }).locator("select").selectOption("ebs_dba");

  // Neither the EBS lookup UI nor any category/scope selection should
  // appear for this persona — access is all-or-nothing.
  await expect(page.locator('button:has-text("Look up user")')).toHaveCount(0);
  await expect(page.locator('input[placeholder="JDOE"]')).toHaveCount(0);
  await expect(page.locator(".org-checkbox-row")).toHaveCount(0);
  await expect(page.locator("text=/full DBA toolset/")).toBeVisible();

  await page.fill('input[placeholder="Senior DBA — patching access"]', "Read-only DBA");
  await page.click('button:has-text("Create mapping")');

  const row = page.locator("tr", { hasText: entraSubject });
  await expect(row).toBeVisible();
  await expect(row.locator("td").nth(2)).toHaveText("EBS — DBA");
  await expect(row.locator("td").nth(3)).toHaveText("—"); // no account — no FND_USER for this persona
  await expect(row.locator(".pill-org")).toHaveText("All DBA tools");
});

test("a functional EBS mapping still requires a username — the DBA exemption doesn't leak", async ({ page }) => {
  await page.goto("/");
  await page.fill("#admin-subject", "e2e.dba@corp.com");
  await page.fill('input[placeholder="j.doe@corp.com"]', subject());
  // Default target system is "ebs" — the lookup button should be present here.
  await expect(page.locator('button:has-text("Look up user")')).toBeVisible();
});
