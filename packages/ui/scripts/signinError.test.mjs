import assert from "node:assert/strict";
import test from "node:test";
import { describeError } from "../src/lib/signinError.ts";

const WAITING = "We’re still setting up your access. Try again in a few minutes.";
const DENIED =
  "That account can’t sign in here: it isn’t on the allow-list for this workspace, or its email isn’t verified. Ask an admin to add you.";

test("the directory's waiting reason shows the still-setting-up text", () => {
  assert.equal(describeError("AccessDenied", "directory_waiting"), WAITING);
});

test("every other denial keeps today's text", () => {
  for (const reason of [undefined, null, "", "Directory_Waiting", "directory_waiting ", "not_in_allowlist", "<b>x</b>"]) {
    assert.equal(describeError("AccessDenied", reason), DENIED, String(reason));
  }
});

test("the reason means nothing with any other error, and is never shown", () => {
  for (const code of ["CredentialsSignin", "Configuration", "OAuthCallbackError", "x"]) {
    const text = describeError(code, "directory_waiting");
    assert.notEqual(text, WAITING);
    assert.ok(!text.includes("directory_waiting"));
  }
  assert.ok(!describeError("<script>alert(1)</script>").includes("<script>"));
});
