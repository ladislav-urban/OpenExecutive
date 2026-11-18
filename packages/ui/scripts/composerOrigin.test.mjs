import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { afterCodeFill, afterInput, afterRestore, sendTyped } from "../src/lib/composerOrigin.ts";
import { settleQueue } from "../src/lib/queuedMessages.ts";

// Chat.tsx's composer, as these helpers drive it: `fromCode` is
// composerFromCodeRef, `queue` the messages typed while a turn works.
function composer(initial = "") {
  const state = { text: initial, fromCode: afterCodeFill(initial), queue: [], sent: [] };
  return {
    state,
    // Code puts words there: an Edit, a follow-up chip, a `?draft=` link.
    fill(text) {
      state.text = text;
      state.fromCode = afterCodeFill(text);
    },
    // The person types or deletes; `inputType` as the browser reports it.
    type(value, inputType = "insertText") {
      state.text = value;
      state.fromCode = afterInput(state.fromCode, value, inputType);
    },
    // Enter: the composer's own Send.
    send() {
      const typed = sendTyped("composer", state.fromCode);
      state.sent.push({ text: state.text, typed });
      state.text = "";
      state.fromCode = afterCodeFill("");
      return typed;
    },
    // Enter while the turn works: queued, with whether it was typed.
    queueWhileWorking() {
      const typed = sendTyped("composer", state.fromCode);
      state.queue.push({ id: String(state.queue.length), text: state.text, taken: false, typed });
      state.text = "";
      state.fromCode = afterCodeFill("");
    },
    // The turn ends with a reply: the leftovers go out as their own turn.
    sendLeftovers() {
      const settled = settleQueue(state.queue);
      state.queue = [];
      return sendTyped("leftovers", state.fromCode, settled.nextTyped);
    },
    // The turn was stopped before any output: its text and the queue go
    // back into the composer.
    restore(turnText, turnTyped) {
      state.text = [turnText, ...state.queue.map((m) => m.text)].filter((t) => t.trim()).join("\n\n");
      state.fromCode = afterRestore(turnTyped, state.queue);
      state.queue = [];
    },
  };
}

test("only the composer's own Send of typed words goes out as typed", () => {
  const box = composer();
  box.type("What if Metro pays late?");
  assert.equal(box.send(), true);
  // A hand-off, a starter prompt, Approve, Try again and an Edit: code.
  assert.equal(sendTyped("code", false), false);
});

test("an Edit of a hand-off's seed sends false, even after typing more", () => {
  const box = composer();
  box.fill("Let's discuss this card:\n\nUpdated forecast for Q1");
  box.type("Let's discuss this card:\n\nUpdated forecast for Q1 please");
  assert.equal(box.send(), false);
});

test("a ?draft= link sent unchanged sends false; cleared and retyped sends true", () => {
  const box = composer("Show me the 13-week cash outlook");
  assert.equal(box.send(), false);
  const again = composer("Show me the 13-week cash outlook");
  again.type("", "deleteContentBackward");
  again.type("Show me the 13-week cash outlook");
  assert.equal(again.send(), true);
});

test("the browser's undo bringing code's words back counts as code", () => {
  const box = composer();
  box.fill("Updated forecast for Q1");
  box.type("", "deleteContentBackward");
  box.type("Updated forecast for Q1", "historyUndo");
  assert.equal(box.send(), false);
  box.type("x", "historyRedo");
  assert.equal(box.state.fromCode, true);
  box.type("", "historyUndo");
  assert.equal(box.state.fromCode, false);
});

test("an accepted follow-up sends false", () => {
  const box = composer();
  box.fill("Want a 13-week cash outlook?");
  assert.equal(box.send(), false);
});

test("Edit, then Try again, then Enter while working: the leftover sends false", () => {
  const box = composer();
  box.fill("Updated forecast for Q1"); // Edit
  assert.equal(sendTyped("code", box.state.fromCode), false); // Try again
  box.queueWhileWorking(); // Enter while it works, the edit still in the box
  assert.equal(box.sendLeftovers(), false);
});

test("a chip accepted mid-run, then sent as a leftover, sends false", () => {
  const box = composer();
  box.fill("Want a 13-week cash outlook?");
  box.queueWhileWorking();
  assert.equal(box.sendLeftovers(), false);
});

test("leftovers send false while the composer holds code's words", () => {
  const box = composer();
  box.type("and what if Metro pays late?");
  box.queueWhileWorking();
  box.fill("Want a 13-week cash outlook?");
  assert.equal(box.sendLeftovers(), false);
});

test("typed leftovers send true", () => {
  const box = composer();
  box.type("and what if Metro pays late?");
  box.queueWhileWorking();
  assert.equal(box.sendLeftovers(), true);
});

test("Try again of a hand-off, stopped and resent, sends false", () => {
  const box = composer();
  const turnTyped = sendTyped("code", box.state.fromCode); // Try again
  box.restore("Updated forecast for Q1", turnTyped); // stopped before any output
  assert.equal(box.send(), false);
});

test("a typed turn stopped with typed words queued comes back typed", () => {
  const box = composer();
  box.type("what if Metro pays late?");
  const turnTyped = box.send();
  box.type("and North?");
  box.queueWhileWorking();
  box.restore("what if Metro pays late?", turnTyped);
  assert.equal(box.send(), true);
});

test("a restore with any part not typed comes back as code", () => {
  const box = composer();
  box.type("what if Metro pays late?");
  const turnTyped = box.send();
  box.fill("Want a 13-week cash outlook?");
  box.queueWhileWorking();
  box.restore("what if Metro pays late?", turnTyped);
  assert.equal(box.send(), false);
});

// Chat.tsx itself: these helpers only hold if every code fill and the one
// send site use them.
const CHAT = readFileSync(new URL("../src/components/Chat.tsx", import.meta.url), "utf8");
const CODE = CHAT.split(/\r?\n/).filter((line) => !line.trim().startsWith("//")).join("\n");

test("Chat.tsx fills the composer from code only through fillComposer", () => {
  const calls = [...CODE.matchAll(/setInput\((.*)\);/g)].map((m) => m[1]);
  assert.deepEqual(calls, [
    // fillComposer itself.
    "text",
    // The person's own typing (handleTextareaChange).
    "e.target.value",
    // The person's own dictation.
    "(prev) => joinDictation(prev, text)",
  ]);
  assert.equal([...CODE.matchAll(/setInput\(/g)].length, 3);
});

test("Chat.tsx sends typed from its one send site, computed by sendTyped", () => {
  const sites = [...CODE.matchAll(/\btyped:\s*([^,\n]+)/g)].map((m) => m[1].trim());
  assert.deepEqual(sites, ["turnTyped"]);
  assert.match(CODE, /const turnTyped = sendTyped\(/);
  assert.match(CODE, /<textarea[\s\S]*?autoComplete="off"/);
});
