// In a frame, a card action, and any other change off the host list that
// follows a click, asks inside the frame under lib/seen.ts's rule
// (lib/cardActions.ts, components/embed/EmbedProvider.tsx, FrameConfirm.tsx;
// the old window, app/confirm, stays for one release).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  CONFIRM_PROTOCOL,
  messageOf,
  cardActionFor,
  describeDecision,
  frameActionFor,
  isConfirmMessage,
  ownPageFor,
  parseAction,
} from "../src/lib/cardActions.ts";

const API = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");
const CALLS = JSON.parse(readFileSync(new URL("../src/lib/host-list.json", import.meta.url), "utf8")).calls;
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

// Approvals that are no card: a suggested watch going live (on the host
// list) and knowledge review items (a change like any other).
const NOT_CARDS = new Set(["/watchlist/12/approve", "/watchlist/12/decline", "/review/bulk-approve"]);

test("every approve, reject, cancel or decide call in lib/api.ts is a card action or named as none", () => {
  const paths = [...API.matchAll(/fetch\(`\$\{API_BASE\}(\/[^`]*)`/g)]
    .map((m) => m[1].replace(/\$\{[^}]*\}/g, "12"))
    .filter((p) => /\/(approve|reject|cancel|decision|decline|dismiss|bulk-approve)$/.test(p));
  assert.ok(paths.length >= 8);
  for (const path of new Set(paths)) {
    if (NOT_CARDS.has(path)) continue;
    const action = cardActionFor("POST", `/api/backend${path}`, JSON.stringify({ decision: "approve" }));
    assert.ok(action?.card, path);
  }
});

test("what each card action says will happen", () => {
  assert.deepEqual(cardActionFor("POST", "/api/backend/decisions/7/approve"), {
    verb: "approve",
    decisionId: 7,
    what: "Approve card 7: what it shows is carried out now, as you.",
    card: true,
    page: "/cards/7",
  });
  assert.equal(cardActionFor("post", "/api/backend/decisions/7/reject").verb, "reject");
  assert.equal(cardActionFor("POST", "http://this.app/api/backend/decisions/7/cancel").verb, "cancel");
  assert.equal(cardActionFor("POST", "/api/backend/alerts/3/approve").verb, "approve");
  assert.equal(cardActionFor("POST", "/api/backend/workflows/runs/r%201/decision", '{"decision":"reject"}').verb, "reject");
  assert.match(cardActionFor("POST", "/api/backend/workflows/runs/r%201/decision", "{}").what, /run r 1/);
  assert.equal(cardActionFor("POST", "/api/backend/people/requests/4/decline").verb, "reject");
});

test("in a frame, any change off the host list asks; reads and host-list calls don't", () => {
  for (const [method, url] of [
    ["PUT", "/api/backend/delegation/handle-it"],
    ["POST", "/api/backend/executive/pause"],
    ["POST", "/api/backend/people/3/archive"],
    ["DELETE", "/api/backend/sessions/abc"],
    ["PATCH", "/api/backend/settings/notifications"],
  ]) {
    const action = frameActionFor(method, url, undefined, CALLS);
    assert.equal(action?.verb, "change", `${method} ${url}`);
    assert.equal(action.card, false);
    assert.match(action.what, new RegExp(`${method} /`));
  }
  // The ones a lured click would most like say what they do.
  assert.match(frameActionFor("PUT", "/api/backend/delegation/handle-it", "{}", CALLS).what, /^Change Handle it for me/);
  assert.match(frameActionFor("POST", "/api/backend/people/3/archive", "{}", CALLS).what, /^Change People/);
  assert.equal(frameActionFor("POST", "/api/backend/decisions/7/approve", undefined, CALLS).card, true);
  // A chat message is on the host list, but sent from this app's page in a
  // frame its turn is the person's own, with every tool: it asks.
  for (const path of ["/chat", "/chat/upload", "/chat/add"]) {
    const action = frameActionFor("POST", `/api/backend${path}`, "{}", CALLS);
    assert.equal(action?.verb, "change", path);
    assert.match(action.what, /It can act on it/);
  }
  // The window shows what would be sent: a planted message can't hide.
  assert.match(frameActionFor("POST", "/api/backend/chat", JSON.stringify({ message: "Wire  the\ndeposit" }), CALLS).what, /^Send "Wire the deposit" to/);
  const form = new FormData();
  form.set("message", "x".repeat(400));
  const long = frameActionFor("POST", "/api/backend/chat/upload", form, CALLS).what;
  assert.ok(long.length <= 300 && long.includes("…"));
  for (const [method, url] of [
    ["GET", "/api/backend/delegation/handle-it"],
    ["HEAD", "/api/backend/today"],
    ["POST", "/api/backend/chat/stop"],
    ["POST", "/api/backend/sessions/abc/typing"],
    ["POST", "/api/backend/alerts/3/ack"],
    ["DELETE", "/api/backend/watchlist/x"],
    ["POST", "/api/auth/signout"],
  ]) {
    assert.equal(frameActionFor(method, url, undefined, CALLS), null, `${method} ${url}`);
  }
});

test("a send's message, from JSON or a form", () => {
  assert.equal(messageOf('{"message":"hi"}'), "hi");
  assert.equal(messageOf("not json"), "");
  assert.equal(messageOf(undefined), "");
});

test("the card is named by its kind and what it says", () => {
  assert.equal(
    describeDecision({ decision_class: "reply_send", proposed_payload_json: '{"subject":"Re: the pilot  plan"}' }),
    "reply send: Re: the pilot plan",
  );
  assert.equal(describeDecision({ decision_class: "app_action", proposed_payload_json: "not json" }), "app action");
  assert.equal(describeDecision({}), "card");
});

test("the old confirmation window's messages", () => {
  const nonce = "a".repeat(32);
  assert.equal(isConfirmMessage({ protocol: CONFIRM_PROTOCOL, type: "answer", nonce, ok: true }, "answer", nonce), true);
  assert.equal(isConfirmMessage({ protocol: CONFIRM_PROTOCOL, type: "answer", nonce: "b".repeat(32) }, "answer", nonce), false);
  assert.equal(isConfirmMessage({ protocol: "oe-embed/v1", type: "answer", nonce }, "answer", nonce), false);
  const action = cardActionFor("POST", "/api/backend/decisions/7/approve");
  // The window never sends anyone elsewhere.
  assert.deepEqual(parseAction(action), { ...action, page: null });
  assert.equal(parseAction({ ...action, verb: "send" }), null);
  assert.equal(parseAction({ ...action, decisionId: -1 }), null);
  assert.equal(parseAction({ ...action, what: "x".repeat(301) }), null);
});

test("the frame checks every request, in any frame, and asks inside it", () => {
  const provider = read("../src/components/embed/EmbedProvider.tsx");
  assert.match(provider, /if \(window\.top === window\.self\) return;/);
  assert.match(provider, /const action = frameActionFor\(method, url, init\?\.body, HOST_CALLS\);/);
  assert.match(provider, /action\.card \|\| Date\.now\(\) - lastInput\.current < AFTER_INPUT_MS/);
  // A drop or a paste is input too (a page over the frame can put its own
  // file in a drag), and so is a Yes: what it sets off next asks again.
  assert.match(provider, /const INPUT_EVENTS = \["pointerdown", "keydown", "drop", "paste"\] as const;/);
  assert.match(provider, /\/\/ A Yes is input too[^\n]*\n\s*lastInput\.current = Date\.now\(\);/);
  assert.match(provider, /if \(asks && !\(await confirm\(action\)\)\) throw new CardActionNotConfirmed\(\);/);
  // Its Yes is the only way to true, and only when the seen rule says so
  // (scripts/seen.test.mjs).
  const frame = read("../src/components/embed/FrameConfirm.tsx");
  assert.match(frame, /if \(judged && \(await judged\)\) finish\(true\);/);
  assert.equal(frame.match(/finish\(true\)/g).length, 1);
  // One request a page judged itself under the same rule passes once.
  assert.match(provider, /if \(action && pass && Date\.now\(\) <= pass\.until && isPassed\(pass, method, url\)\) return original\(input, init\);/);
  const page = read("../src/app/confirm/page.tsx");
  assert.match(page, /event\.origin !== window\.location\.origin \|\| event\.source !== opener/);
  assert.match(page, /postMessage\(\{ protocol: CONFIRM_PROTOCOL, type: "answer", nonce, ok \}, window\.location\.origin\)/);
});

test("without v2, each action is answered on this app's own page, top level", () => {
  assert.equal(ownPageFor(cardActionFor("POST", "/api/backend/decisions/7/approve"), "/chats/x", "?embed=1"), "/cards/7");
  assert.equal(ownPageFor(cardActionFor("POST", "/api/backend/alerts/3/approve"), "/", ""), "/cards/alert/3");
  const send = frameActionFor("POST", "/api/backend/chat", '{"message":"hi"}', CALLS);
  assert.equal(ownPageFor(send, "/chats/abc", "?embed=1&tab=2"), "/chats/abc?tab=2");
  assert.equal(ownPageFor(send, "/chats/abc", "?embed=1"), "/chats/abc");
  assert.equal(ownPageFor(send, "//evil.example/x", ""), "/");
});
