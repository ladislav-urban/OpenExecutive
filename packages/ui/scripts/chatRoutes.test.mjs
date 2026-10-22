import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import {
  BRIEFING_PANELS,
  NEW_CHAT_PATH,
  briefingPanelPath,
  chatHomeView,
  chatPath,
  isBriefingPanel,
  isChatHomePath,
  legacyHomeRedirect,
  newChatPath,
} from "../src/lib/chatRoutes.ts";
import { APP_ROUTES, isAppPath, isNativePath } from "../src/lib/embed.ts";

const SRC = new URL("../src/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

test("a conversation and a new chat have addresses of their own", () => {
  assert.equal(chatPath("abc 1"), "/chats/abc%201");
  assert.equal(chatPath("abc", "Q3 budget"), "/chats/abc?find=Q3%20budget");
  assert.equal(newChatPath(), NEW_CHAT_PATH);
  assert.equal(newChatPath("Revise this"), "/chats/new?draft=Revise%20this");
  assert.equal(newChatPath("x".repeat(5000)).length, "/chats/new?draft=".length + 2000);
});

test("the old Home addresses redirect, keeping draft, find and embed", () => {
  assert.equal(legacyHomeRedirect("?session=abc"), "/chats/abc");
  assert.equal(legacyHomeRedirect("?session=abc&find=renewal"), "/chats/abc?find=renewal");
  assert.equal(legacyHomeRedirect("?new=1"), "/chats/new");
  assert.equal(legacyHomeRedirect("?new=1&draft=Hello%20there"), "/chats/new?draft=Hello%20there");
  assert.equal(legacyHomeRedirect("?session=abc&embed=1"), "/chats/abc?embed=1");
  assert.equal(legacyHomeRedirect("?new=1&embed=1"), "/chats/new?embed=1");
  assert.equal(legacyHomeRedirect(""), null);
  assert.equal(legacyHomeRedirect("?embed=1"), null);
  assert.equal(legacyHomeRedirect("?session="), null);
});

test("the chat home's addresses, and nothing else, are the chat home", () => {
  assert.deepEqual(chatHomeView("/"), { kind: "briefing" });
  assert.deepEqual(chatHomeView("/chats/new"), { kind: "new" });
  assert.deepEqual(chatHomeView("/chats/abc%201"), { kind: "chat", sessionId: "abc 1" });
  for (const other of ["/chats", "/chats/shared", "/chats/shared/abc", "/chats/abc/more", "/today", "/chats/%E0%A4%A"]) {
    assert.equal(chatHomeView(other), null, other);
    assert.equal(isChatHomePath(other), false, other);
  }
});

test("with Home native in a host, a conversation still opens in its frame", () => {
  const native = ["/"];
  assert.equal(isNativePath("/", native), true);
  assert.equal(isNativePath(chatPath("abc"), native), false);
  assert.equal(isNativePath(NEW_CHAT_PATH, native), false);
  for (const panel of BRIEFING_PANELS) assert.equal(isNativePath(briefingPanelPath(panel), native), false);
  // A host may list them as native later (step 7): they are this app's paths.
  assert.ok(isAppPath("/chats/new") && isAppPath("/briefing/handled"));
  assert.ok(APP_ROUTES.includes("briefing") && APP_ROUTES.includes("chats"));
});

// Every .ts / .tsx file under src/.
function sources(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

test("no link to the old /?session= or /?new=1 addresses is left", () => {
  const stale = /["'`]\/\?(session|new)=/;
  const offenders = sources(SRC).filter((path) => stale.test(readFileSync(path, "utf8")));
  assert.deepEqual(offenders, []);
});

test("every panel the API can name has a page, and Home builds each one", () => {
  // The tile keys and summary panels GET /today sends (briefing/home_rules.py).
  const rules = readFileSync(new URL("../../core/openexecutive/briefing/home_rules.py", import.meta.url), "utf8");
  const literal = (name) => {
    const match = new RegExp(`${name}: Literal\\[([^\\]]+)\\]`).exec(rules);
    assert.ok(match, name);
    return [...match[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  };
  const tileKeys = literal("key");
  const summaryPanels = literal("panel");
  for (const key of [...tileKeys, ...summaryPanels]) assert.ok(isBriefingPanel(key), key);
  assert.ok(isBriefingPanel("brief"));
  assert.equal(isBriefingPanel("weeklyReview"), false);

  const briefing = readFileSync(new URL("../src/components/Briefing.tsx", import.meta.url), "utf8");
  const built = /const HOME_PANEL_KEYS[^=]*=\s*\[([^\]]+)\]/.exec(briefing);
  assert.ok(built);
  assert.deepEqual([...built[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]), [...BRIEFING_PANELS]);
  for (const key of BRIEFING_PANELS) {
    assert.match(briefing, new RegExp(`\\n\\s+${key}: \\{\\n?\\s*title:`), `Home builds the ${key} panel`);
  }
});
