// Old addresses (lib/pageAliases.ts) keep their query on the way to the page
// they now open, `?embed=1` above all: a host's frame opened at one of them
// otherwise lands on the standalone page and never hears the host.
//
// next.config.ts can't be imported here (Next compiles it), so this checks the
// aliases it serves, through the function Next's router applies to a config
// redirect (server/lib/router-utils/resolve-routes.js), and that no page file
// is left at an alias (a page-level `redirect()` there dropped the query).
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { PAGE_ALIASES } from "../src/lib/pageAliases.ts";

const require = createRequire(import.meta.url);
const { prepareDestination } = require("next/dist/shared/lib/router/utils/prepare-destination.js");

function follow(destination, query) {
  const { parsedDestination } = prepareDestination({ appendParamsToQuery: false, destination, params: {}, query });
  return { pathname: parsedDestination.pathname, query: parsedDestination.query };
}

for (const { source, destination } of PAGE_ALIASES) {
  test(`${source} keeps ?embed=1 on the way to ${destination}`, () => {
    const landed = follow(destination, { embed: "1", tab: "x" });
    const target = new URL(destination, "http://app");
    assert.equal(landed.pathname, target.pathname);
    assert.equal(landed.query.embed, "1");
    assert.equal(landed.query.tab, "x");
    for (const [key, value] of target.searchParams) assert.equal(landed.query[key], value);
  });

  test(`${source} is served only by the redirect`, () => {
    // A page file at the alias would never be reached, and would invite a
    // `redirect()` that drops the query back in.
    const page = new URL(`../src/app${source}/page.tsx`, import.meta.url);
    assert.equal(existsSync(page), false, `${source}/page.tsx`);
  });
}

test("next.config.ts serves the aliases as redirects", () => {
  const config = readFileSync(new URL("../next.config.ts", import.meta.url), "utf8");
  assert.match(config, /redirects\(\)\s*\{\s*return PAGE_ALIASES\.map/);
});
