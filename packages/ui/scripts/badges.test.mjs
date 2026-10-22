import assert from "node:assert/strict";
import test from "node:test";

import { reviewBadgeFrom } from "../src/lib/badges.ts";
import { badgeMessage, EMBED_PROTOCOL } from "../src/lib/embed.ts";

test("the sidebar's Knowledge badge is the review queue's count", () => {
  assert.equal(reviewBadgeFrom({ home: 4, knowledge: 5, watchlist: 2 }, 0), 5);
  assert.equal(reviewBadgeFrom({ knowledge: 0 }, 7), 0);
});

test("a review count the API left out keeps the badge as it was", () => {
  assert.equal(reviewBadgeFrom({ home: 1, watchlist: 2 }, 3), 3);
  assert.equal(reviewBadgeFrom({ knowledge: -1 }, 3), 3);
});

test("an embedded page hands the host GET /badges's counts as they are", () => {
  assert.deepEqual(badgeMessage({ home: 1, knowledge: 5, watchlist: 2 }), {
    protocol: EMBED_PROTOCOL,
    type: "badge",
    counts: { home: 1, knowledge: 5, watchlist: 2 },
  });
  // A count that failed is simply not there.
  assert.deepEqual(badgeMessage({ home: 1 }).counts, { home: 1 });
});
