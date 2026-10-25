// Approving a card from a host's screen (lib/hostApprove.ts): the inline
// Approve (app/embed/approve/[id]) and the old window it replaces
// (app/confirm/alert/[id]): which addresses they take, that they approve
// only what the card offers, what they say after, and who may frame them.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { EMBED_PROTOCOL, MAX_APPROVE_HEIGHT, isApproveAsk, sizeMessage } from "../src/lib/embed.ts";
import { ARM_DELAY_MS, alertIdFrom, approvedLine, clickCounts, offersApprove } from "../src/lib/hostApprove.ts";
import { neverFramed } from "../src/lib/middlewarePaths.ts";

test("the window takes a plain alert id and nothing else", () => {
  assert.equal(alertIdFrom("28"), 28);
  for (const bad of ["", "0", "-1", "28a", "2.5", "1e3", "../28", "12345678901", null, undefined]) {
    assert.equal(alertIdFrom(bad), null, String(bad));
  }
});

test("it approves only a card that offers this person Approve", () => {
  assert.equal(offersApprove([{ key: "approve" }, { key: "discuss" }]), true);
  assert.equal(offersApprove([{ key: "approve_with_edits" }, { key: "discuss" }]), false);
  assert.equal(offersApprove([]), false);
  assert.equal(offersApprove(undefined), false);
});

test("it says what happened", () => {
  assert.equal(approvedLine("decision"), "Approved.");
  assert.match(approvedLine("alert", "running"), /new chat/);
  assert.equal(approvedLine("alert", "failed", "It couldn't be queued."), "It couldn't be done: It couldn't be queued.");
});

test("its Approve waits long enough to miss a lured second click", () => {
  assert.ok(ARM_DELAY_MS >= 500);
});

test("a click counts only when pressed after Approve armed", () => {
  assert.equal(clickCounts(1000, 1500), true);
  assert.equal(clickCounts(1000, 1000), true);
  // Pressed while disabled, released after it armed.
  assert.equal(clickCounts(1000, 900), false);
  assert.equal(clickCounts(null, 1500), false);
  assert.equal(clickCounts(1000, null), false);
});

test("Approve is offered only on the host's ask for this very card", () => {
  const ask = { protocol: EMBED_PROTOCOL, type: "approve-ask", alertId: 28 };
  assert.equal(isApproveAsk(ask, 28), true);
  assert.equal(isApproveAsk(ask, 29), false);
  assert.equal(isApproveAsk({ ...ask, alertId: "28" }, 28), false);
  assert.equal(isApproveAsk({ ...ask, type: "approve-ready" }, 28), false);
  assert.equal(isApproveAsk({ ...ask, protocol: "oe-embed/v2" }, 28), false);
  assert.equal(isApproveAsk({ ...ask, extra: 1 }, 28), false);
  assert.equal(isApproveAsk(null, 28), false);
});

test("no page may frame a confirmation window", () => {
  for (const path of ["/confirm", "/confirm/", "/confirm/alert/28", "/%63onfirm/alert/28", "/confirm%2Falert/28", "/%E0%A4%A"]) {
    assert.ok(neverFramed(path), path);
  }
  for (const path of ["/cards/alert/28", "/confirmations", "/", "/chats/confirm"]) {
    assert.ok(!neverFramed(path), path);
  }
});

// The inline Approve a host places (app/embed/approve/[id]), which replaces
// the window above.
const INLINE = readFileSync(new URL("../src/app/embed/approve/[id]/page.tsx", import.meta.url), "utf8");

test("a host may place the inline Approve in a frame", () => {
  for (const path of ["/embed/approve/28", "/embed/approve/28/"]) assert.ok(!neverFramed(path), path);
});

test("the inline Approve offers Approve only on the host's ask for this card, from its parent", () => {
  assert.match(INLINE, /acceptsEvent\(event, hostOrigins, parent\) \|\| !isApproveAsk\(event\.data, id\)/);
  assert.match(INLINE, /const canApprove = proposal \? host !== null && offersApprove/);
  // Watching (and so arming) starts only once a host asked.
  assert.match(INLINE, /if \(host === null \|\| !panel\.current\) return;\s*const check/);
});

test("the inline Approve counts a click only under the seen rule, and opens no window", () => {
  assert.match(INLINE, /const counts = await watch\.current\.judge\(\{ isTrusted: event\.nativeEvent\.isTrusted, target: event\.target \}\);/);
  assert.match(INLINE, /const isApprove = \(target: unknown\) => target !== null && target === button\.current;/);
  assert.doesNotMatch(INLINE, /window\.open\(/);
  // Without v2: the card's own page, top level in the same tab.
  assert.match(INLINE, /target="_top"/);
});

test("the size it asks for is bounded", () => {
  assert.deepEqual(sizeMessage(120.2), { protocol: EMBED_PROTOCOL, type: "size", height: 121 });
  assert.equal(sizeMessage(10_000).height, MAX_APPROVE_HEIGHT);
  assert.equal(sizeMessage(-5).height, 0);
  assert.equal(sizeMessage(Number.NaN).height, 0);
});

test("only the inline Approve lets a request past the frame's gate, and only after its own judging", async () => {
  const { readdirSync, statSync } = await import("node:fs");
  const root = new URL("../src/", import.meta.url);
  const users = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const url = new URL(name, dir);
      if (statSync(url).isDirectory()) walk(new URL(`${name}/`, dir));
      else if (/\.tsx?$/.test(name) && /usePassOnce\(/.test(readFileSync(url, "utf8"))) users.push(url.pathname);
    }
  };
  walk(root);
  assert.deepEqual(
    users.map((p) => decodeURIComponent(p).replace(/^.*\/src\//, "")),
    ["app/embed/approve/[id]/page.tsx", "components/embed/EmbedProvider.tsx"].sort(),
  );
  // passOnce is called only after judge said yes.
  const judged = INLINE.indexOf("if (!counts) return;");
  assert.ok(judged > 0 && INLINE.indexOf("passOnce(") > judged);
});
