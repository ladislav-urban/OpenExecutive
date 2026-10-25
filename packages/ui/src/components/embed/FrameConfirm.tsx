"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { buttonClass } from "@/components/ui/Button";
import { ownPageFor, type CardAction } from "@/lib/cardActions";
import { SeenGate, tracksVisibility, watchPanel } from "@/lib/seen";

// In a frame, a card action, a chat message or another change asks here,
// inside the frame, naming what will happen. The page around the frame
// could hide or cover it and lure clicks, so its Yes counts only once the
// browser has reported this panel seen and the pointer has moved in it
// (lib/seen.ts). A browser that can't report that offers no Yes: the person
// opens this app's own page for it, top level in the same tab. Cancel is No.

const YES: Record<CardAction["verb"], string> = {
  approve: "Yes, approve",
  reject: "Yes, dismiss",
  cancel: "Yes, cancel it",
  change: "Yes, go ahead",
};

export default function FrameConfirm({
  action,
  onAnswer,
}: {
  action: CardAction;
  onAnswer: (ok: boolean) => void;
}) {
  const panel = useRef<HTMLDivElement | null>(null);
  const yes = useRef<HTMLButtonElement | null>(null);
  const gate = useRef(new SeenGate());
  const flush = useRef<(() => void) | null>(null);
  const answered = useRef(false);
  // Shown only in the browser (EmbedProvider asks from a request), so
  // `window` is there. False where the browser can't tell it is seen.
  const [supported] = useState(() => tracksVisibility(window));
  const [ownPage] = useState(() => ownPageFor(action, window.location.pathname, window.location.search));
  const [ready, setReady] = useState(false);

  const finish = useCallback(
    (ok: boolean) => {
      if (answered.current) return;
      answered.current = true;
      onAnswer(ok);
    },
    [onAnswer],
  );

  useEffect(() => {
    const check = () => setReady(gate.current.clickCounts({ isTrusted: true, onYes: true, now: performance.now() }));
    const watch = panel.current ? watchPanel(window, panel.current, gate.current, check) : null;
    if (!watch) return;
    flush.current = watch.flush;
    // The run of "seen" grows without a new report, so look again meanwhile.
    const timer = window.setInterval(check, 100);
    return () => {
      window.clearInterval(timer);
      watch.stop();
      flush.current = null;
    };
  }, []);

  const onYes = (event: React.MouseEvent<HTMLButtonElement>) => {
    flush.current?.();
    const counts = gate.current.clickCounts({
      isTrusted: event.nativeEvent.isTrusted,
      onYes: event.target === yes.current,
      now: performance.now(),
    });
    if (counts) finish(true);
  };

  const danger = action.verb === "reject" || action.verb === "cancel";

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 px-4">
      <div
        ref={panel}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="frame-confirm-title"
        onPointerMove={(event) => gate.current.pointerMoved(event.nativeEvent.isTrusted)}
        className="w-full max-w-md rounded-2xl border border-line bg-surface-elevated p-5 shadow-xl"
      >
        <h2 id="frame-confirm-title" className="text-base font-semibold text-fg">
          Are you sure?
        </h2>
        <p className="mt-2 text-sm text-fg">{action.what}</p>
        {!supported && (
          <p className="mt-2 text-xs text-fg-subtle">
            This page is shown inside another one, and this browser can&rsquo;t tell whether this question is really
            on screen, so answer it on Open Executive&rsquo;s own page. Your browser&rsquo;s Back brings you here again.
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={() => finish(false)} className={buttonClass("secondary", "sm")}>
            Cancel
          </button>
          {!supported ? (
            <a href={ownPage} target="_top" rel="noopener" className={buttonClass("primary", "sm")}>
              Open it here
            </a>
          ) : (
            <button
              ref={yes}
              type="button"
              onClick={onYes}
              disabled={!ready}
              className={buttonClass(danger ? "danger" : "primary", "sm")}
            >
              {YES[action.verb]}
            </button>
          )}
        </div>
        {supported && !ready && (
          <p className="mt-2 text-right text-xs text-fg-subtle">Point at this box to answer.</p>
        )}
      </div>
    </div>
  );
}
