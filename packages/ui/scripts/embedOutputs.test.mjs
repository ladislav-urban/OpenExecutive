// The build outputs for host apps (scripts/embed-outputs.mjs).
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";
import { isAppPath } from "../src/lib/embed.ts";
import { embedManifest, hostList } from "./embed-outputs.mjs";

test("the manifest names every destination and hub tab by a route of this app", () => {
  const manifest = embedManifest();
  assert.equal(manifest.protocol, "oe-embed/v1");
  assert.equal(manifest.contract, 1);
  for (const mode of ["team", "solo"]) {
    const { destinations, hubs } = manifest.navigation[mode];
    assert.ok(destinations.length >= 5, mode);
    for (const href of [...destinations.map((d) => d.href), ...hubs.flatMap((h) => h.tabs.map((t) => t.href))]) {
      assert.ok(isAppPath(href.split(/[?#]/)[0]), `${mode}: ${href}`);
    }
  }
});

test("the host list goes out as it is", () => {
  const list = hostList();
  assert.ok(Array.isArray(list.calls) && list.calls.length > 0);
});

test("the manifest names the inline Approve a host places, at a page this app has", () => {
  const { approve } = embedManifest().pieces;
  assert.equal(approve.path, "/embed/approve/{alertId}");
  assert.equal(approve.query, "?embed=1");
  assert.ok(approve.maxHeight > 0);
  assert.ok(existsSync(new URL("../src/app/embed/approve/[id]/page.tsx", import.meta.url)));
});

test("the manifest names the one-pass sign-in, at a route this app has", () => {
  const { signIn } = embedManifest().pieces;
  assert.equal(signIn.path, "/signin/sso");
  assert.equal(signIn.failed, "oe_signin=failed");
  assert.equal(signIn.replacesOtherUser, true);
  for (const route of ["sso", "return"]) {
    assert.ok(existsSync(new URL(`../src/app/signin/${route}/route.ts`, import.meta.url)), route);
  }
});

test("the manifest names the sign-out a host's page passes through, at a route this app has", () => {
  const { signOut } = embedManifest().pieces;
  assert.deepEqual(signOut, { path: "/signout/sso", query: "?to={hostPage}" });
  assert.ok(existsSync(new URL("../src/app/signout/sso/route.ts", import.meta.url)));
});
