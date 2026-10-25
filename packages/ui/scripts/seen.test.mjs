// A Yes inside a frame counts only once the browser reported it seen
// (lib/seen.ts): FrameConfirm.tsx and the inline Approve
// (app/embed/approve/[id]) use it, with the observer's entries faked here.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { OBSERVE_DELAY_MS, SEEN_MS, SeenGate, tracksVisibility, watchPanel } from "../src/lib/seen.ts";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

const seen = (time, extra = {}) => ({ isIntersecting: true, intersectionRatio: 1, isVisible: true, time, ...extra });
const yes = (now, extra = {}) => ({ isTrusted: true, onYes: true, now, ...extra });

/** A gate whose panel was seen from t=0 and pointed at. */
function armedGate() {
  const gate = new SeenGate();
  gate.observe(seen(0));
  gate.pointerMoved(true);
  return gate;
}

test("a Yes counts once the panel was seen long enough, pointed at, and clicked for real", () => {
  assert.equal(armedGate().clickCounts(yes(SEEN_MS)), true);
  assert.ok(SEEN_MS >= 700);
});

test("a Yes is refused when the panel was seen for under SEEN_MS", () => {
  assert.equal(armedGate().clickCounts(yes(SEEN_MS - 1)), false);
});

test("a Yes is refused while the panel is covered, see-through, transformed or hidden", () => {
  // The browser reports all of these as not visible; off screen as not intersecting.
  for (const report of [
    { isVisible: false },
    { isIntersecting: false, intersectionRatio: 0 },
    { intersectionRatio: 0.5 },
    { isVisible: undefined },
  ]) {
    const gate = armedGate();
    gate.observe(seen(100, report));
    assert.equal(gate.clickCounts(yes(5_000)), false, JSON.stringify(report));
  }
});

test("a break starts the run again, and the pointer must move again after it", () => {
  const gate = armedGate();
  gate.observe(seen(400, { isVisible: false }));
  gate.observe(seen(500));
  assert.equal(gate.clickCounts(yes(500 + SEEN_MS)), false, "no move since the break");
  gate.pointerMoved(true);
  assert.equal(gate.clickCounts(yes(500 + SEEN_MS - 1)), false, "the run counts from the break");
  assert.equal(gate.clickCounts(yes(500 + SEEN_MS)), true);
});

test("a Yes is refused without pointer movement", () => {
  const gate = new SeenGate();
  gate.observe(seen(0));
  assert.equal(gate.clickCounts(yes(5_000)), false);
});

test("pointer movement counts only when trusted and while the panel is seen", () => {
  const untrusted = new SeenGate();
  untrusted.observe(seen(0));
  untrusted.pointerMoved(false);
  assert.equal(untrusted.clickCounts(yes(5_000)), false);

  const early = new SeenGate();
  early.pointerMoved(true);
  early.observe(seen(0));
  assert.equal(early.clickCounts(yes(5_000)), false, "moved before it was seen");
});

test("a Yes is refused without a trusted click on the button itself", () => {
  assert.equal(armedGate().clickCounts(yes(5_000, { isTrusted: false })), false);
  assert.equal(armedGate().clickCounts(yes(5_000, { onYes: false })), false);
});

test("only an observer that reports visibility counts as v2", () => {
  class IO {}
  assert.equal(tracksVisibility({ IntersectionObserver: IO, IntersectionObserverEntry: { prototype: { isVisible: false } } }), true);
  assert.equal(tracksVisibility({ IntersectionObserver: IO, IntersectionObserverEntry: { prototype: {} } }), false);
  assert.equal(tracksVisibility({ IntersectionObserver: IO }), false);
  assert.equal(tracksVisibility({}), false);
  assert.equal(tracksVisibility(null), false);
});

test("without v2 nothing is watched, so nothing can arm", () => {
  class IO {}
  const gate = new SeenGate();
  assert.equal(watchPanel({ IntersectionObserver: IO, IntersectionObserverEntry: { prototype: {} } }, {}, gate, () => {}), null);
  assert.equal(gate.clickCounts(yes(5_000)), false);
});

test("with v2 the panel is watched for visibility, and pending reports are read at the click", () => {
  let made;
  class IO {
    constructor(cb, options) {
      made = { cb, options, observed: [], pending: [] };
      this.state = made;
    }
    observe(el) {
      this.state.observed.push(el);
    }
    disconnect() {
      this.state.stopped = true;
    }
    takeRecords() {
      const out = this.state.pending;
      this.state.pending = [];
      return out;
    }
  }
  const win = { IntersectionObserver: IO, IntersectionObserverEntry: { prototype: { isVisible: true } } };
  const gate = new SeenGate();
  let changes = 0;
  const panel = {};
  const watch = watchPanel(win, panel, gate, () => changes++);
  assert.deepEqual(made.options, { threshold: [1.0], trackVisibility: true, delay: OBSERVE_DELAY_MS });
  assert.deepEqual(made.observed, [panel]);
  made.cb([seen(0)]);
  gate.pointerMoved(true);
  assert.equal(gate.clickCounts(yes(SEEN_MS)), true);
  // Covered just before the click, not yet delivered: flush reads it.
  made.pending.push(seen(SEEN_MS - 10, { isVisible: false }));
  watch.flush();
  assert.equal(gate.clickCounts(yes(SEEN_MS + 10)), false);
  assert.ok(changes >= 2);
  watch.stop();
  assert.equal(made.stopped, true);
});

test("the in-frame confirm opens no window and, without v2, sends the person to this app's own page", () => {
  const src = read("../src/components/embed/FrameConfirm.tsx");
  assert.doesNotMatch(src, /window\.open\(/);
  assert.match(src, /watchPanel\(/);
  assert.match(src, /clickCounts\(\{\s*isTrusted: event\.nativeEvent\.isTrusted/);
  assert.match(src, /target="_top"/);
});
