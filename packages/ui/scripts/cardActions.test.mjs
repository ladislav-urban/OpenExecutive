// In a frame, approving, rejecting or cancelling a card asks twice
// (lib/cardActions.ts, components/embed/EmbedProvider.tsx).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { cardActionFor, describeDecision } from "../src/lib/cardActions.ts";

const API = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");

// Approvals that are no card: a suggested watch going live (a host may do
// that too, it is on the host list) and knowledge review items.
const NOT_CARDS = new Set(["/watchlist/12/approve", "/watchlist/12/decline", "/review/bulk-approve"]);

test("every approve, reject, cancel or decide call in lib/api.ts is a card action or named as none", () => {
  const paths = [...API.matchAll(/fetch\(`\$\{API_BASE\}(\/[^`]*)`/g)]
    .map((m) => m[1].replace(/\$\{[^}]*\}/g, "12"))
    .filter((p) => /\/(approve|reject|cancel|decision|decline|dismiss|bulk-approve)$/.test(p));
  assert.ok(paths.length >= 8);
  for (const path of new Set(paths)) {
    if (NOT_CARDS.has(path)) continue;
    assert.ok(cardActionFor("POST", `/api/backend${path}`, JSON.stringify({ decision: "approve" })), path);
  }
});

test("what each card action says will happen", () => {
  assert.deepEqual(cardActionFor("POST", "/api/backend/decisions/7/approve"), {
    verb: "approve",
    decisionId: 7,
    what: "Approve card 7: what it shows is carried out now, as you.",
  });
  assert.equal(cardActionFor("post", "/api/backend/decisions/7/reject").verb, "reject");
  assert.equal(cardActionFor("POST", "http://this.app/api/backend/decisions/7/cancel").verb, "cancel");
  assert.equal(cardActionFor("POST", "/api/backend/alerts/3/approve").verb, "approve");
  assert.equal(cardActionFor("POST", "/api/backend/workflows/runs/r%201/decision", '{"decision":"reject"}').verb, "reject");
  assert.match(cardActionFor("POST", "/api/backend/workflows/runs/r%201/decision", "{}").what, /run r 1/);
  assert.equal(cardActionFor("POST", "/api/backend/people/requests/4/decline").verb, "reject");
});

test("reads and everything else go straight through", () => {
  for (const [method, url] of [
    ["GET", "/api/backend/decisions/7"],
    ["GET", "/api/backend/decisions/7/approve"],
    ["POST", "/api/backend/chat"],
    ["POST", "/api/backend/watchlist/x/approve"],
    ["POST", "/decisions/7/approve"],
    ["POST", "/api/backend/decisions/x/approve"],
  ]) {
    assert.equal(cardActionFor(method, url), null, `${method} ${url}`);
  }
});

test("the card is named by its kind and what it says", () => {
  assert.equal(
    describeDecision({ decision_class: "reply_send", proposed_payload_json: '{"subject":"Re: the pilot  plan"}' }),
    "reply send: Re: the pilot plan",
  );
  assert.equal(describeDecision({ decision_class: "app_action", proposed_payload_json: "not json" }), "app action");
  assert.equal(describeDecision({}), "card");
});

test("the frame checks every request the page sends, in any frame", () => {
  const provider = readFileSync(new URL("../src/components/embed/EmbedProvider.tsx", import.meta.url), "utf8");
  assert.match(provider, /if \(window\.top === window\.self\) return;/);
  assert.match(provider, /const action = cardActionFor\(method, url, init\?\.body\);/);
  assert.match(provider, /if \(action && !\(await confirm\(action\)\)\) throw new CardActionNotConfirmed\(\);/);
});
