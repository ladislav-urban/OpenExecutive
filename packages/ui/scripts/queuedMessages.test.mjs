import assert from "node:assert/strict";
import test from "node:test";
import { allTyped, composerText, markTaken, settleQueue } from "../src/lib/queuedMessages.ts";

const q = [
  { id: "a", text: "make it Friday", taken: false, typed: true },
  { id: "b", text: "and cc Dana", taken: false, typed: true },
];

test("markTaken flags only the named messages", () => {
  const out = markTaken(q, ["b"]);
  assert.deepEqual(out.map((m) => m.taken), [false, true]);
  assert.equal(markTaken(q, []), q);
});

test("taken messages stay with the reply, the rest become the next turn", () => {
  const settled = settleQueue(markTaken(q, ["a"]));
  assert.deepEqual(settled.taken, ["make it Friday"]);
  assert.equal(settled.next, "and cc Dana");
});

test("nothing left means no next turn", () => {
  assert.equal(settleQueue(markTaken(q, ["a", "b"])).next, null);
  assert.deepEqual(settleQueue([]), { taken: [], next: null, nextTyped: false });
});

test("several leftovers are sent as one message, in order", () => {
  assert.equal(settleQueue(q).next, "make it Friday\n\nand cc Dana");
});

test("composerText returns everything to the composer, skipping blanks", () => {
  assert.equal(composerText("book the room", q), "book the room\n\nmake it Friday\n\nand cc Dana");
  assert.equal(composerText("", q), "make it Friday\n\nand cc Dana");
});

test("leftovers are typed only if every part of them was", () => {
  assert.equal(settleQueue(q).nextTyped, true);
  const chip = { id: "c", text: "Want a 13-week cash outlook?", taken: false, typed: false };
  assert.equal(settleQueue([...q, chip]).nextTyped, false);
  // A part the turn took is not in the leftovers, so it doesn't count.
  assert.equal(settleQueue(markTaken([...q, chip], ["c"])).nextTyped, true);
  assert.equal(allTyped([...q, { id: "d", text: "  ", taken: false, typed: false }]), true);
  assert.equal(allTyped([chip]), false);
});
