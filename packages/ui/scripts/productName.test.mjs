// What this app calls itself on its own pages (lib/productName.ts, OE_PRODUCT_NAME).
import assert from "node:assert/strict";
import test from "node:test";

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { DEFAULT_PRODUCT_NAME, PRODUCT, productName, withProduct } from "../src/lib/productName.ts";

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

test("copy writes the name in for each PRODUCT token", () => {
  assert.equal(PRODUCT, "{product}");
  assert.equal(withProduct(`Pause ${PRODUCT}`, "Northwind"), "Pause Northwind");
  assert.equal(withProduct(`${PRODUCT} is answering ${PRODUCT}`, "N"), "N is answering N");
  // A name is written as it is: "$&" is no pattern here.
  assert.equal(withProduct(`Ask ${PRODUCT}`, "A$&B"), "Ask A$&B");
  assert.equal(withProduct("No token", "N"), "No token");
});

// The files that write the token out because they import types only.
for (const file of ["components/shell/navConfig.ts", "lib/firstRun.ts", "lib/whoSends.ts", "lib/cardActions.ts"]) {
  test(`${file} writes the same token`, () => {
    const text = readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
    assert.match(text, /const PRODUCT = "\{product\}";/);
  });
}

// The UI names the product (OE_PRODUCT_NAME), never "the Executive": copy that
// says "the Executive", "your Executive" or "Open Executive" is a regression.
// Comments, imports and identifiers aside; DEFAULT_PRODUCT_NAME is the name.
test("no screen says the Executive or Open Executive", () => {
  const src = new URL("../src/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
  const found = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.(ts|tsx)$/.test(name) && !name.includes(".test.")) {
        readFileSync(path, "utf8").split(/\r?\n/).forEach((line, i) => {
          const code = line.trim();
          if (/^(\/\/|\*|\/\*|\{\/\*|import )/.test(code)) return;
          if (/\b(?:[Tt]he|[Yy]our) Executive\b|Open Executive/.test(code.replace(/DEFAULT_PRODUCT_NAME = "Open Executive"|product = "Open Executive"/, ""))) {
            found.push(`${path.slice(src.length)}:${i + 1}: ${code.slice(0, 100)}`);
          }
        });
      }
    }
  };
  walk(src);
  assert.deepEqual(found, []);
});
