// A made draft's "Open in ERPNext" (lib/erpnextLinks.ts): in a host's frame,
// a document on the host's own origin opens in its desk; anything else stays
// a new-tab link.
import assert from "node:assert/strict";
import test from "node:test";
import { openMessage } from "../src/lib/embed.ts";
import { erpnextLink } from "../src/lib/erpnextLinks.ts";

const HOST = "http://erp.dynocortex.localhost:8088";
const DOC = `${HOST}/app/material-request/MAT-MR-2026-00012`;

test("in a frame, the host's own document is opened by the host", () => {
  const link = erpnextLink(DOC, HOST);
  assert.deepEqual(link, { kind: "host", route: "/app/material-request/MAT-MR-2026-00012" });
  // What the card sends the host instead of navigating.
  assert.deepEqual(openMessage(link), {
    protocol: "oe-embed/v1",
    type: "open",
    kind: "host",
    route: "/app/material-request/MAT-MR-2026-00012",
  });
  assert.deepEqual(erpnextLink(`${HOST}/desk/todo/abc?x=1`, HOST), { kind: "host", route: "/desk/todo/abc?x=1" });
});

test("another origin, or outside a frame, stays a new-tab link", () => {
  for (const [href, origin] of [
    [DOC, null],
    [DOC, "https://erp.example.com"],
    [DOC.replace("http:", "https:"), HOST],
    [DOC.replace(":8088", ":8089"), HOST],
    ["https://evil.example/app/x", HOST],
  ]) {
    assert.deepEqual(erpnextLink(href, origin), { kind: "tab", href });
  }
});

test("only a desk route is sent; the host refuses anything else", () => {
  for (const path of ["/", "/login", "/api/method/frappe.auth.get_logged_user", "/files/x.pdf", "/desk//x"]) {
    const href = `${HOST}${path}`;
    assert.equal(erpnextLink(href, HOST).kind, "tab", path);
  }
  assert.equal(erpnextLink(`http://user:pw@erp.dynocortex.localhost:8088/app/x`, HOST).kind, "tab");
  assert.equal(erpnextLink("not a url", HOST).kind, "tab");
});
