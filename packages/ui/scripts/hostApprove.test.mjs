// The window a host's screen opens to approve a card (lib/hostApprove.ts,
// app/confirm/alert/[id]): which addresses it takes, that it approves only
// what the card offers, what it says after, and that no page may frame it.
import assert from "node:assert/strict";
import test from "node:test";

import { ARM_DELAY_MS, alertIdFrom, approvedLine, offersApprove } from "../src/lib/hostApprove.ts";
import { NEVER_FRAMED_PATH } from "../src/lib/middlewarePaths.ts";

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

test("no page may frame a confirmation window", () => {
  for (const path of ["/confirm", "/confirm/", "/confirm/alert/28"]) assert.ok(NEVER_FRAMED_PATH.test(path), path);
  for (const path of ["/cards/alert/28", "/confirmations", "/", "/chats/confirm"]) {
    assert.ok(!NEVER_FRAMED_PATH.test(path), path);
  }
});
