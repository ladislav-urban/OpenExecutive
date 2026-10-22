"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { useHostOrigins } from "@/components/embed/EmbedProvider";
import Button, { buttonClass } from "@/components/ui/Button";
import { approveAlert, approveDecision, getAlertCard, type AlertCard } from "@/lib/api";
import { acceptsEvent, approveReadyMessage, cardDoneMessage, isApproveAsk, type CardOutcome } from "@/lib/embed";
import { ARM_DELAY_MS, alertIdFrom, approvedLine, clickCounts, offersApprove } from "@/lib/hostApprove";

// The window a host's own screen opens to have one of Home's cards approved
// (lib/hostApprove.ts): this app's page, never in a frame, showing the card
// to whoever is signed in here. It offers Approve only when a listed host
// that opened it asks, and only its own Approve approves; then it tells the
// host which card is done and closes.

const CLOSE_AFTER_MS = 2500;

const noSubscribe = () => () => {};

export default function ApproveAlertWindow() {
  const params = useParams<{ id: string }>();
  const id = alertIdFrom(params?.id);
  const hostOrigins = useHostOrigins();
  // undefined while loading, null when it isn't the caller's or isn't waiting.
  const [card, setCard] = useState<AlertCard | null | undefined>(undefined);
  const framed = useSyncExternalStore(noSubscribe, () => window.top !== window.self, () => false);
  // The host origin that asked for this card, once one has.
  const [host, setHost] = useState<string | null>(null);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const armedAt = useRef<number | null>(null);
  const pressedAt = useRef<number | null>(null);

  useEffect(() => {
    if (id === null) return;
    const controller = new AbortController();
    getAlertCard(id, controller.signal)
      .then(setCard)
      .catch(() => setCard(null));
    return () => controller.abort();
  }, [id]);

  // The handshake: `approve-ready` to the opener at each listed host origin
  // (the browser delivers it only to the one the opener is), then
  // `approve-ask` for this card from that opener and origin alone.
  useEffect(() => {
    const opener = window.opener as Window | null;
    if (id === null || !opener || hostOrigins.length === 0) return;
    const onMessage = (event: MessageEvent) => {
      if (!acceptsEvent(event, hostOrigins, opener) || !isApproveAsk(event.data, id)) return;
      setHost((current) => current ?? event.origin);
    };
    window.addEventListener("message", onMessage);
    for (const origin of hostOrigins) opener.postMessage(approveReadyMessage(id), origin);
    return () => window.removeEventListener("message", onMessage);
  }, [id, hostOrigins]);

  // Armed only after the window has been in front for a moment and the
  // pointer has then moved inside it, and again after every time it lost
  // focus: a click meant for the page that opened it must not land here.
  useEffect(() => {
    if (host === null) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let waited = false;
    const disarm = () => {
      clearTimeout(timer);
      waited = false;
      armedAt.current = null;
      setArmed(false);
    };
    const wait = () => {
      disarm();
      if (document.visibilityState === "visible" && document.hasFocus()) {
        timer = setTimeout(() => {
          waited = true;
        }, ARM_DELAY_MS);
      }
    };
    const onMove = (event: PointerEvent) => {
      if (!event.isTrusted || !waited || armedAt.current !== null) return;
      armedAt.current = Date.now();
      setArmed(true);
    };
    wait();
    window.addEventListener("focus", wait);
    window.addEventListener("blur", disarm);
    document.addEventListener("visibilitychange", wait);
    window.addEventListener("pointermove", onMove);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("focus", wait);
      window.removeEventListener("blur", disarm);
      document.removeEventListener("visibilitychange", wait);
      window.removeEventListener("pointermove", onMove);
    };
  }, [host]);

  // The host that asked learns which card is done. Nothing else crosses.
  const tellHost = (outcome: CardOutcome) => {
    const opener = window.opener as Window | null;
    if (opener && host && id !== null) opener.postMessage(cardDoneMessage(id, outcome), host);
  };

  const approve = async () => {
    if (!card || busy || !clickCounts(armedAt.current, pressedAt.current)) return;
    const p = card.proposal;
    setBusy(true);
    setFailed(null);
    try {
      let line: string;
      if (p.decision_instance_id != null) {
        await approveDecision(p.decision_instance_id);
        line = approvedLine("decision");
      } else {
        const approval = await approveAlert(p.alert_id);
        line = approvedLine("alert", approval.status, approval.outcome);
      }
      setDone(line);
      tellHost("approved");
      setTimeout(() => window.close(), CLOSE_AFTER_MS);
    } catch (e) {
      setFailed(e instanceof Error ? e.message : "That didn't work. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const notNow = () => {
    tellHost("not_now");
    window.close();
  };

  const proposal = card?.proposal;
  const canApprove = proposal ? host !== null && offersApprove(proposal.actions) : false;

  return (
    <main className="min-h-screen bg-surface px-5 py-6 text-fg">
      <div className="mx-auto max-w-md space-y-3">
        {framed ? (
          <p className="text-sm text-fg-muted">This window can&rsquo;t be shown inside another page.</p>
        ) : id === null || card === null ? (
          <p className="text-sm text-fg-muted">
            This card isn&rsquo;t here: it may be someone else&rsquo;s, or no longer waiting.
          </p>
        ) : card === undefined || !proposal ? (
          <p className="text-sm text-fg-muted">Loading the card…</p>
        ) : done ? (
          <p className="text-sm text-fg" role="status">
            {done}
          </p>
        ) : (
          <>
            <h1 className="text-base font-semibold">Approve this?</h1>
            <p className="text-sm font-medium">{proposal.headline}</p>
            {(proposal.recommended_move || proposal.suggested_action) && (
              <p className="text-sm">{proposal.recommended_move || proposal.suggested_action}</p>
            )}
            {proposal.body && <p className="whitespace-pre-line text-sm text-fg-muted line-clamp-6">{proposal.body}</p>}
            {canApprove ? (
              <>
                {proposal.decision_instance_id == null && (
                  <p className="text-xs text-fg-subtle">The Executive then does it as you, in a new chat.</p>
                )}
                <div className="flex justify-end gap-2 pt-2">
                  <button type="button" className={buttonClass("secondary")} onClick={notNow} disabled={busy}>
                    Not now
                  </button>
                  <Button
                    variant="primary"
                    onPointerDown={() => {
                      pressedAt.current = Date.now();
                    }}
                    onKeyDown={() => {
                      pressedAt.current = Date.now();
                    }}
                    onClick={() => void approve()}
                    disabled={!armed || busy}
                  >
                    {busy ? "Approving…" : "Approve"}
                  </Button>
                </div>
              </>
            ) : (
              <p className="text-sm text-fg-muted">
                <Link href={`/cards/alert/${proposal.alert_id}`} className="text-accent hover:underline">
                  Open this card in Open Executive
                </Link>{" "}
                to answer it.
              </p>
            )}
          </>
        )}
        {failed && (
          <p className="text-sm text-rose-700 dark:text-rose-300" role="alert">
            {failed}
          </p>
        )}
      </div>
    </main>
  );
}
