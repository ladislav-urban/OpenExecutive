"use client";

import { useParams } from "next/navigation";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { useHostOrigins, usePassOnce } from "@/components/embed/EmbedProvider";
import { buttonClass } from "@/components/ui/Button";
import { approveAlert, approveDecision, getAlertCard, type AlertCard } from "@/lib/api";
import {
  acceptsEvent,
  approveReadyMessage,
  cardDoneMessage,
  isApproveAsk,
  sizeMessage,
  type CardOutcome,
} from "@/lib/embed";
import { alertIdFrom, approvedLine, offersApprove } from "@/lib/hostApprove";
import { SeenGate, tracksVisibility, watchPanel, type Watch } from "@/lib/seen";

// The inline Approve a host places on its own screen for one of Home's cards
// (lib/hostApprove.ts): a small frame of this app's own, showing the card's
// own words, from here, to whoever is signed in here. The host only places
// it. It offers Approve only once the host that placed it asks for this
// card (`approve-ready`, then `approve-ask`), and its Approve counts only
// under lib/seen.ts's rule. A browser that can't report the frame seen gets
// a link to the card's own page instead, top level in the same tab, and so
// does anyone who can't point. It keeps this app's own colours and face
// (`.consent`, globals.css), whatever the host's theme.

const noSubscribe = () => () => {};

export default function InlineApprove() {
  const params = useParams<{ id: string }>();
  const id = alertIdFrom(params?.id);
  const hostOrigins = useHostOrigins();
  const passOnce = usePassOnce();
  // undefined while loading, null when it isn't the caller's or isn't waiting.
  const [card, setCard] = useState<AlertCard | null | undefined>(undefined);
  const framed = useSyncExternalStore(noSubscribe, () => window.top !== window.self, () => false);
  // The host origin that asked for this card, once one has.
  const [host, setHost] = useState<string | null>(null);
  // False where the browser can't tell the frame is seen.
  const supported = useSyncExternalStore(noSubscribe, () => tracksVisibility(window), () => true);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const panel = useRef<HTMLElement | null>(null);
  const button = useRef<HTMLButtonElement | null>(null);
  const gate = useRef(new SeenGate());
  const watch = useRef<Watch | null>(null);
  const judging = useRef(false);

  useEffect(() => {
    if (id === null) return;
    const controller = new AbortController();
    getAlertCard(id, controller.signal)
      .then(setCard)
      .catch(() => setCard(null));
    return () => controller.abort();
  }, [id]);

  // The handshake: `approve-ready` to the parent at each listed host origin
  // (the browser delivers it only to the one the parent is), then
  // `approve-ask` for this card from that parent and origin alone.
  useEffect(() => {
    if (id === null || window.top === window.self || hostOrigins.length === 0) return;
    const parent = window.parent;
    const onMessage = (event: MessageEvent) => {
      if (!acceptsEvent(event, hostOrigins, parent) || !isApproveAsk(event.data, id)) return;
      setHost((current) => current ?? event.origin);
    };
    window.addEventListener("message", onMessage);
    for (const origin of hostOrigins) parent.postMessage(approveReadyMessage(id), origin);
    return () => window.removeEventListener("message", onMessage);
  }, [id, hostOrigins]);

  // The host learns how tall to make the frame, and nothing else.
  useEffect(() => {
    const el = panel.current;
    if (!host || !el) return;
    const send = () => window.parent.postMessage(sizeMessage(el.getBoundingClientRect().height), host);
    const observer = new ResizeObserver(send);
    observer.observe(el);
    send();
    return () => observer.disconnect();
  }, [host, card, done, failed, supported]);

  // Approve arms only under lib/seen.ts's rule, from the host's ask on.
  useEffect(() => {
    if (host === null || !panel.current) return;
    const check = () => setReady(watch.current?.armed() ?? false);
    const isApprove = (target: unknown) => target !== null && target === button.current;
    const watching = watchPanel(window, panel.current, isApprove, gate.current, check);
    if (!watching) return;
    watch.current = watching;
    const timer = window.setInterval(check, 100);
    return () => {
      window.clearInterval(timer);
      watching.stop();
      watch.current = null;
    };
  }, [host]);

  const tellHost = (outcome: CardOutcome) => {
    if (host && id !== null) window.parent.postMessage(cardDoneMessage(id, outcome), host);
  };

  const approve = async (event: React.MouseEvent<HTMLButtonElement>) => {
    if (!card || busy || judging.current || !watch.current) return;
    judging.current = true;
    const counts = await watch.current.judge({ isTrusted: event.nativeEvent.isTrusted, target: event.target });
    judging.current = false;
    if (!counts) return;
    const p = card.proposal;
    setBusy(true);
    setFailed(null);
    try {
      let line: string;
      // Judged here under the same rule, so the frame's own gate
      // (EmbedProvider) doesn't ask a second time for this one request.
      if (p.decision_instance_id != null) {
        passOnce("POST", `/api/backend/decisions/${p.decision_instance_id}/approve`);
        await approveDecision(p.decision_instance_id);
        line = approvedLine("decision");
      } else {
        passOnce("POST", `/api/backend/alerts/${p.alert_id}/approve`);
        const approval = await approveAlert(p.alert_id);
        line = approvedLine("alert", approval.status, approval.outcome);
      }
      setDone(line);
      tellHost("approved");
    } catch (e) {
      setFailed(e instanceof Error ? e.message : "That didn't work. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const proposal = card?.proposal;
  const cardPage = proposal ? `/cards/alert/${proposal.alert_id}` : id !== null ? `/cards/alert/${id}` : "/";
  const canApprove = proposal ? host !== null && offersApprove(proposal.actions) : false;

  return (
    <main
      ref={panel}
      className="consent bg-surface px-4 py-3 text-fg"
    >
      {!framed ? (
        <p className="text-sm text-fg-muted">
          This is shown inside another page.{" "}
          <a href={cardPage} className="text-accent hover:underline">
            Open the card
          </a>
        </p>
      ) : id === null || card === null ? (
        <div className="flex items-start justify-between gap-2">
          <p className="text-sm text-fg-muted">This card isn&rsquo;t here: it may be someone else&rsquo;s, or no longer waiting.</p>
          {host !== null && id !== null && (
            <button type="button" className={buttonClass("secondary", "sm")} onClick={() => tellHost("not_now")}>
              Not now
            </button>
          )}
        </div>
      ) : card === undefined || !proposal ? (
        <p className="text-sm text-fg-muted">Loading the card…</p>
      ) : done ? (
        <p className="text-sm text-fg" role="status">
          {done}
        </p>
      ) : (
        <div className="space-y-2">
          <p className="text-sm font-medium">{proposal.headline}</p>
          {(proposal.recommended_move || proposal.suggested_action) && (
            <p className="text-sm">{proposal.recommended_move || proposal.suggested_action}</p>
          )}
          {canApprove && supported ? (
            <>
              {proposal.decision_instance_id == null && (
                <p className="text-xs text-fg-subtle">The Executive then does it as you, in a new chat.</p>
              )}
              <div className="flex items-center justify-end gap-2">
                <a href={cardPage} target="_top" rel="noopener" className="mr-auto text-xs text-accent hover:underline">
                  Open the card
                </a>
                {!ready && !busy && <span className="text-xs text-fg-subtle">Hold the pointer here to approve.</span>}
                <button type="button" className={buttonClass("secondary", "sm")} onClick={() => tellHost("not_now")} disabled={busy}>
                  Not now
                </button>
                <button
                  ref={button}
                  type="button"
                  className={buttonClass("primary", "sm")}
                  onClick={(event) => void approve(event)}
                  disabled={!ready || busy}
                >
                  {busy ? "Approving…" : "Approve"}
                </button>
              </div>
            </>
          ) : (
            // The host hid the card's own buttons while this is open, so a way
            // back out is always here, not only beside an armed Approve.
            <div className="flex items-start justify-between gap-2">
              <p className="text-sm text-fg-muted">
                <a href={cardPage} target="_top" rel="noopener" className="text-accent hover:underline">
                  Open this card in Open Executive
                </a>{" "}
                to answer it{!supported ? ": this browser can't show here that Approve is really on screen" : ""}.
              </p>
              {host !== null && (
                <button type="button" className={buttonClass("secondary", "sm")} onClick={() => tellHost("not_now")}>
                  Not now
                </button>
              )}
            </div>
          )}
        </div>
      )}
      {failed && (
        <p className="mt-2 text-sm text-rose-700 dark:text-rose-300" role="alert">
          {failed}
        </p>
      )}
    </main>
  );
}
