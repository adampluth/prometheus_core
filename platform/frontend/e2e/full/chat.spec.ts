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

/* A chat turn through the whole platform: the browser posts it, nginx proxies
 * it, the backend relays it to the agent over A2A and streams the status back
 * over SSE, and the reply settles in the transcript. The e2e stack's stub agent
 * answers, so the reply is known in advance. */
import { expect, test } from "./fixtures";

const STUB_ANSWER = "This is the e2e stub agent.";

test("a message gets the agent's reply, and the chat joins the history", async ({ page }) => {
  await page.goto("/");
  const prompt = `Hello from the e2e test ${Date.now()}`;

  await page.getByPlaceholder("Message...").fill(prompt);
  await page.keyboard.press("Enter");

  const main = page.getByRole("main");
  await expect(main.getByText(prompt)).toBeVisible();
  await expect(main.getByText(STUB_ANSWER, { exact: false })).toBeVisible();
  await expect(page.getByLabel("Assistant is thinking")).toHaveCount(0);
  await expect(page.getByPlaceholder("Message...")).toBeEnabled();
});
