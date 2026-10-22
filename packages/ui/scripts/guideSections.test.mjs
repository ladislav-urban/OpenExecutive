import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { GUIDE_SECTION_IDS, readyToScroll, sectionFromSearch } from "../src/lib/guideSections.ts";
import { embedManifest } from "./embed-outputs.mjs";

// The API's registry of the guide's sections (guide/sections.py GUIDE_SECTIONS).
const registry = [
  ...readFileSync(new URL("../../core/openexecutive/guide/sections.py", import.meta.url), "utf8").matchAll(
    /^\s+id="([a-z_]+)",$/gm,
  ),
].map((m) => m[1]);

test("the page's sections are the API's, in its order", () => {
  assert.ok(registry.length > 10);
  assert.deepEqual([...GUIDE_SECTION_IDS], registry);
});

test("the manifest lists every section a host may link to", () => {
  assert.deepEqual(embedManifest().guideSections, registry);
});

test("?section= opens at a known section; anything else opens at the top", () => {
  assert.equal(sectionFromSearch("?section=watchlist"), "watchlist");
  // As a host's frame opens it.
  assert.equal(sectionFromSearch("?section=company_profile&embed=1"), "company_profile");
  assert.equal(sectionFromSearch("?embed=1&section=today"), "today");
  assert.equal(sectionFromSearch("?section=nope"), null);
  assert.equal(sectionFromSearch("?section="), null);
  assert.equal(sectionFromSearch(""), null);
});

test("it scrolls once the section and every one above it have loaded", () => {
  const settled = new Set(["chat", "ask_oe"]);
  assert.equal(readyToScroll("today", settled), false);
  settled.add("today");
  // A section below doesn't matter; one above still loading does.
  assert.equal(readyToScroll("today", settled), true);
  assert.equal(readyToScroll("pulse", new Set(["pulse", "chat", "ask_oe"])), false);
  assert.equal(readyToScroll("chat", new Set(["chat"])), true);
  assert.equal(readyToScroll("nope", settled), false);
});
