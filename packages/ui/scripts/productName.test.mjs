// What this app calls itself on its own pages (lib/productName.ts, OE_PRODUCT_NAME).
import assert from "node:assert/strict";
import test from "node:test";

import { DEFAULT_PRODUCT_NAME, productName } from "../src/lib/productName.ts";

test("an install's own name is used as plain text", () => {
  assert.equal(productName("Northwind"), "Northwind");
  // Whitespace, a line break too, reads as one space.
  assert.equal(productName("  Northwind   Ops "), "Northwind Ops");
  assert.equal(productName("Line\nbreak"), "Line break");
});

test("unset, too long or not plain text, it is this app's own name", () => {
  for (const raw of [undefined, null, "", "   ", "x".repeat(41), "<b>Evil</b>", "A & B", 'Say "hi"', "Bell\u0007"]) {
    assert.equal(productName(raw), DEFAULT_PRODUCT_NAME, JSON.stringify(raw));
  }
});
