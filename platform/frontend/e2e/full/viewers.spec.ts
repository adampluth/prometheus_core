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

/* Each artifact viewer, opened the way a user gets one: by asking in chat. The
 * stub agent returns a sample (e2e stub-client-agent/samples): an STL the backend
 * converts to the mesh viewer's VTP, an IFC model the browser loads through the
 * artifact content proxy, and a Mermaid diagram. Each test checks the viewer
 * actually drew something, not just that its frame appeared.
 *
 * The IFC viewer loads web-ifc and its worker from unpkg, so it needs network
 * access. */
import type { Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures";

async function ask(page: Page, prompt: string, answer: string): Promise<Locator> {
  await page.goto("/");
  await page.getByPlaceholder("Message...").fill(prompt);
  await page.keyboard.press("Enter");
  const main = page.getByRole("main");
  await expect(main.getByText(answer)).toBeVisible();
  return main;
}

/**
 * How many sampled pixels of a viewer's canvas differ from its background, as
 * it shows on screen. A viewer that mounted but drew nothing reads 0. It works
 * from a screenshot, because a WebGL canvas can't be read back once the frame is
 * shown (VTK.js doesn't keep its drawing buffer). The page decodes the PNG; the
 * background is the color most of the top and bottom edge agrees on, where a
 * centered model never reaches.
 */
async function paintedPixels(page: Page, canvas: Locator): Promise<number> {
  const png = (await canvas.screenshot()).toString("base64");
  return page.evaluate(async (base64) => {
    const image = new Image();
    image.src = `data:image/png;base64,${base64}`;
    await image.decode();
    const copy = document.createElement("canvas");
    copy.width = image.width;
    copy.height = image.height;
    const ctx = copy.getContext("2d")!;
    ctx.drawImage(image, 0, 0);
    const data = ctx.getImageData(0, 0, copy.width, copy.height).data;
    const rgb = (i: number) => [data[i], data[i + 1], data[i + 2]];
    const differs = (a: number[], b: number[]) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]) > 30;

    const edge: number[][] = [];
    for (let x = 0; x < copy.width; x += 8) {
      edge.push(rgb(x * 4), rgb(((copy.height - 1) * copy.width + x) * 4));
    }
    const background = edge.reduce((best, color) =>
      edge.filter((c) => !differs(c, color)).length > edge.filter((c) => !differs(c, best)).length ? color : best,
    );

    let painted = 0;
    for (let i = 0; i < data.length; i += 4 * 16) if (differs(rgb(i), background)) painted++;
    return painted;
  }, png);
}

test("a mesh opens in the mesh viewer and draws", async ({ page }) => {
  const main = await ask(page, "show me the mesh", "Here is the mesh.");

  await expect(main.getByText("cube.stl")).toBeVisible();
  await expect(main.getByRole("button", { name: "Reset camera" })).toBeVisible();
  const canvas = main.locator("canvas").first();
  await expect(canvas).toBeVisible();
  await expect.poll(() => paintedPixels(page, canvas), { timeout: 30_000 }).toBeGreaterThan(100);
});

test("an IFC model opens in the IFC viewer, draws, and lists its elements", async ({ page }) => {
  const main = await ask(page, "open the ifc model", "Here is the IFC model.");

  // The visibility panel lists what the model holds: four walls and a floor slab
  await expect(main.getByText("Walls", { exact: true })).toBeVisible({ timeout: 60_000 });
  await expect(main.getByText("Floors", { exact: true })).toBeVisible();
  const canvas = main.locator("canvas").first();
  await expect.poll(() => paintedPixels(page, canvas), { timeout: 60_000 }).toBeGreaterThan(100);
});

test("a Mermaid diagram opens in the diagram viewer with every node", async ({ page }) => {
  const main = await ask(page, "draw the diagram", "Here is the diagram.");

  const diagram = main.locator("svg[id^='mermaid']").first();
  await expect(diagram).toBeVisible();
  for (const node of ["Upload mesh", "Format?", "VTK pipeline", "VTP manifest", "MeshViewer"]) {
    await expect(diagram.getByText(node, { exact: true })).toBeVisible();
  }
});
