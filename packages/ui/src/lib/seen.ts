// Whether a button inside a frame was really seen when it was clicked
// (components/embed/FrameConfirm.tsx, app/embed/approve/[id]).
//
// A page that frames this app (a host origin; frame-ancestors lets no other
// site) can't read or script the frame, but it can place it, resize it, make
// it invisible, cover it and lure clicks onto it. IntersectionObserver v2
// (`trackVisibility: true`, Chromium) answers, from inside the frame, whether
// an element is on screen uncovered, at full opacity, untransformed and
// unfiltered, and the page around can't fake that answer: the browser
// computes it. So a Yes in a frame counts only when:
//
// - the panel holding it has been reported whole on screen and visible,
//   without a break, for at least SEEN_MS. A lured second click on a hidden
//   frame fails this; so does a cover that lets clicks through
//   (`pointer-events: none`), and a frame cropped to show the Yes without
//   the words that say what it does;
// - the real pointer has moved inside the panel while it was visible (a
//   cursor drawn by the page elsewhere doesn't move it);
// - the click is trusted and on the Yes button itself.
//
// The browser reports a change at most every OBSERVE_DELAY_MS, so the gate
// takes the observer's pending records at the click too (`takeRecords`).
//
// A browser without v2 (Firefox, Safari) can't tell, so the frame never
// offers a Yes there: it sends the person to this app's own page, top level
// in the same tab, where nothing can frame it. Never fail open.
//
// No imports, so `npm test` can exercise this with faked entries.

export const SEEN_MS = 700;
// The smallest delay trackVisibility accepts.
export const OBSERVE_DELAY_MS = 100;

/** What the gate reads from an IntersectionObserverEntry. */
export type SeenEntry = { isIntersecting: boolean; intersectionRatio: number; isVisible?: boolean; time: number };

// Whole on screen, allowing for rounding.
const WHOLE = 0.99;

/** Whether this browser's IntersectionObserver can report visibility (v2). */
export function tracksVisibility(win: unknown): boolean {
  const w = win as { IntersectionObserver?: unknown; IntersectionObserverEntry?: { prototype?: object } } | null;
  if (!w || typeof w.IntersectionObserver !== "function") return false;
  const proto = w.IntersectionObserverEntry?.prototype;
  return typeof proto === "object" && proto !== null && "isVisible" in proto;
}

export class SeenGate {
  // When the panel was last reported visible after a break, or null.
  private visibleSince: number | null = null;
  // Whether the real pointer has moved inside the panel since then.
  private moved = false;

  /** An observer entry for the panel: anything short of visible breaks the run. */
  observe(entry: SeenEntry): void {
    if (entry.isIntersecting && entry.intersectionRatio >= WHOLE && entry.isVisible === true) {
      if (this.visibleSince === null) this.visibleSince = entry.time;
    } else {
      this.visibleSince = null;
      this.moved = false;
    }
  }

  /** A pointer move over the panel; counts only trusted, while visible. */
  pointerMoved(isTrusted: boolean): void {
    if (isTrusted && this.visibleSince !== null) this.moved = true;
  }

  /** Whether the panel has been visible for long enough, at `now` (the observer's clock). */
  seenFor(now: number): boolean {
    return this.visibleSince !== null && now - this.visibleSince >= SEEN_MS;
  }

  /** Whether a click counts: trusted, on Yes itself, after the panel was seen and pointed at. */
  clickCounts(click: { isTrusted: boolean; onYes: boolean; now: number }): boolean {
    return click.isTrusted && click.onYes && this.moved && this.seenFor(click.now);
  }
}

type Observer = { observe(el: Element): void; disconnect(): void; takeRecords(): SeenEntry[] };
type ObserverCtor = new (
  cb: (entries: SeenEntry[]) => void,
  options: { threshold: number[]; trackVisibility: boolean; delay: number },
) => Observer;

/**
 * Watch `panel` with v2 and feed `gate`; `changed` is called on every
 * report, so a caller can re-render when Yes may count. Returns `flush`
 * (feed any pending reports now, before judging a click) and `stop`, or
 * null where v2 is missing.
 */
export function watchPanel(
  win: unknown,
  panel: Element,
  gate: SeenGate,
  changed: () => void,
): { flush: () => void; stop: () => void } | null {
  if (!tracksVisibility(win)) return null;
  const Ctor = (win as { IntersectionObserver: ObserverCtor }).IntersectionObserver;
  const feed = (entries: SeenEntry[]) => {
    for (const entry of entries) gate.observe(entry);
    changed();
  };
  const observer = new Ctor(feed, { threshold: [1.0], trackVisibility: true, delay: OBSERVE_DELAY_MS });
  observer.observe(panel);
  return {
    flush: () => feed(observer.takeRecords()),
    stop: () => observer.disconnect(),
  };
}
