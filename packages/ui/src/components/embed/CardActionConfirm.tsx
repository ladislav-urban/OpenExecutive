"use client";

import { useEffect, useRef, useState } from "react";

import Button, { buttonClass } from "@/components/ui/Button";
import { describeDecision, type CardAction } from "@/lib/cardActions";

// The second click a card action needs in a frame (lib/cardActions.ts). It
// names what will happen, and the card it is on, and its Yes button only
// works a moment after it appears: a click already on its way (one a page
// over the frame lured out) lands before then, on nothing.
const ARM_AFTER_MS = 800;

const YES: Record<CardAction["verb"], string> = {
  approve: "Yes, approve",
  reject: "Yes, dismiss",
  cancel: "Yes, cancel it",
};

export default function CardActionConfirm({
  action,
  productName,
  onAnswer,
}: {
  action: CardAction;
  productName: string | null;
  onAnswer: (ok: boolean) => void;
}) {
  const [armed, setArmed] = useState(false);
  const [card, setCard] = useState<string | null>(null);
  const noRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setArmed(true), ARM_AFTER_MS);
    noRef.current?.focus();
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (action.decisionId === null) return;
    const controller = new AbortController();
    fetch(`/api/backend/decisions/${action.decisionId}`, { signal: controller.signal })
      .then((res) => (res.ok ? res.json() : null))
      .then((instance) => {
        if (instance) setCard(describeDecision(instance));
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [action.decisionId]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onAnswer(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onAnswer]);

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 px-4">
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="card-action-confirm-title"
        aria-describedby="card-action-confirm-what"
        className="w-full max-w-md rounded-2xl border border-line bg-surface-elevated p-5 shadow-xl"
      >
        <h2 id="card-action-confirm-title" className="text-base font-semibold text-fg">
          Are you sure?
        </h2>
        <p id="card-action-confirm-what" className="mt-2 text-sm text-fg">
          {action.what}
        </p>
        {card && <p className="mt-2 text-sm text-fg-muted">The card: {card}</p>}
        <p className="mt-3 text-xs text-fg-subtle">
          {productName ? `${productName} asks` : "This page asks"} twice, because it is shown inside another page.
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button ref={noRef} type="button" className={buttonClass("secondary")} onClick={() => onAnswer(false)}>
            No
          </button>
          <Button
            variant={action.verb === "approve" ? "primary" : "danger"}
            disabled={!armed}
            onClick={(event) => {
              if (armed && event.isTrusted) onAnswer(true);
            }}
          >
            {YES[action.verb]}
          </Button>
        </div>
      </div>
    </div>
  );
}
