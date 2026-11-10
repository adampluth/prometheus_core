// This file is part of Prometheus
// https://inl.gov/super-intelligence/prometheus/
//
// Copyright 2026 Battelle Energy Alliance, LLC
// All rights reserved, see NOTICE.txt for full restrictions
// https://github.com/idaholab/prometheus_core/blob/main/NOTICE.txt
//
// Licensed under the MIT License, please see LICENSE for details
// https://github.com/idaholab/prometheus_core/blob/main/LICENSE
//
// SPDX-License-Identifier: MIT

/* Signs in through nginx against the real backend, then opens each main screen
 * from the sidebar. A screen that fails to load, or any page error or failed
 * /api request on the way, fails the test. */
import { expect, test } from "./fixtures";
import { E2E_ADMIN } from "./global-setup";

// [sidebar label, a heading or text that only that screen shows]
const SCREENS: [string, string][] = [
  ["Agent Registry", "Agent Registry"],
  ["Workflows", "Workflow Console"],
  ["Approval Queue", "Human-in-the-Loop Checkpoint Console"],
  ["Project Management", "Access Control & Team Hub"],
  ["Data Management", "Data Sources & Artifact Persistence"],
  ["Admin", "Admin"],
  ["Home", "Home"],
];

test.use({ storageState: { cookies: [], origins: [] } });

test("a user signs in through the sign-in form", async ({ page }) => {
  await page.goto("/");

  await page.getByRole("textbox", { name: "Username" }).fill(E2E_ADMIN.username);
  await page.getByLabel("Password").fill(E2E_ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByPlaceholder("Message...")).toBeVisible();
  await expect(page.getByText(E2E_ADMIN.username, { exact: true })).toBeVisible();
});

test.describe("signed in", () => {
  test.use({ storageState: "test-results-full/.auth/admin.json" });

  test("each main screen loads from the sidebar", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByPlaceholder("Message...")).toBeVisible();

    for (const [label, title] of SCREENS) {
      await page.getByRole("button", { name: label, exact: true }).first().click();
      await expect(page.getByRole("heading", { level: 1, name: title }), `the ${label} screen`).toBeVisible();
    }
  });

  test("a page reloaded on a screen's address falls back to the app", async ({ page }) => {
    // nginx serves index.html for any path, so a deep link opens the app
    const response = await page.goto("/workflows/some/deep/link");
    expect(response?.status()).toBe(200);
    await expect(page.getByPlaceholder("Message...")).toBeVisible();
  });
});
