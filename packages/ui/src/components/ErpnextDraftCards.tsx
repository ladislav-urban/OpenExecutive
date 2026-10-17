"use client";

import { useCallback, useEffect, useState } from "react";

import Button from "@/components/ui/Button";
import {
  approveErpnextDraft,
  dismissErpnextDraft,
  getErpnextDraftCards,
  type ErpnextDraftCard,
  type ErpnextDraftCreated,
} from "@/lib/api";
import { formatRelativeTime } from "@/lib/relativeTime";

// Home and chat: ERPNext drafts the Executive prepared at your request (a
// ToDo, a Material Request), shown as cards in "Needs you". Each card shows
// exactly what will be written, as ERPNext itself read it when it checked the
// draft; that text is ERPNext's, shown as plain text and never as markup.
// Approve makes it once, unsubmitted, as your own ERPNext user; you review
// and submit it in ERPNext. GET /erpnext/drafts answers only the card's own
// person, so nothing shows for anyone else or on a backend without it.

export function useErpnextDraftCards() {
  const [cards, setCards] = useState<ErpnextDraftCard[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    getErpnextDraftCards(controller.signal)
      .then(setCards)
      .catch(() => { /* no card is better than a broken one */ });
    return () => controller.abort();
  }, []);

  const gone = useCallback((decisionId: number) => {
    setCards((prev) => prev.filter((c) => c.decision_id !== decisionId));
  }, []);

  return { cards, gone };
}

// What the draft cards are, for the Needs you header's tip.
export const ERPNEXT_DRAFTS_TIP =
  "ERPNext drafts you asked the Executive to prepare. Each shows exactly what will be written, " +
  "as ERPNext checked it. Nothing is created until you tap Approve, and then only an unsubmitted " +
  "draft, as your own ERPNext user, for you to review and submit in ERPNext. Only you see these.";

export function ErpnextDraftCardItem({
  card,
  onGone,
  emphasized = false,
}: {
  card: ErpnextDraftCard;
  emphasized?: boolean;
  onGone: (id: number) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(card.last_error ? `Last try: ${card.last_error}` : null);
  const [created, setCreated] = useState<ErpnextDraftCreated | null>(null);
  const [done, setDone] = useState(false);
  const when = formatRelativeTime(card.created_at);

  const approve = async () => {
    setBusy("Creating the draft…");
    setError(null);
    try {
      setCreated(await approveErpnextDraft(card.decision_id));
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't make the draft.");
    } finally {
      setBusy(null);
    }
  };

  const dismiss = async () => {
    setBusy("Dismissing…");
    setError(null);
    try {
      await dismissErpnextDraft(card.decision_id);
      onGone(card.decision_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't dismiss that draft.");
      setBusy(null);
    }
  };

  const link = created?.url && /^https?:\/\//.test(created.url) ? created.url : null;
  return (
    <article
      className={`rounded-2xl border bg-surface-elevated p-4 sm:p-5 ${
        emphasized ? "border-accent/60 ring-1 ring-accent/25 shadow-sm" : "border-line"
      }`}
    >
      <div className="mb-2 flex items-center gap-2">
        <span className="inline-flex items-center rounded-lg bg-surface-overlay px-2 py-0.5 text-[13px] font-medium text-fg-muted">
          To approve
        </span>
        <span className="text-[12px] font-medium text-fg-muted">ERPNext {card.doctype} draft</span>
        {when && <span className="text-sm text-fg-subtle tabular-nums">{when}</span>}
      </div>
      {card.why && (
        <div className="text-base sm:text-[17px] font-semibold leading-snug text-fg break-words">{card.why}</div>
      )}

      <div className="mt-3 rounded-xl border border-line bg-surface px-3 py-2">
        <span className="block text-xs font-semibold uppercase tracking-wide text-fg-muted">
          What will be written, as ERPNext checked it
        </span>
        <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          {card.fields.map((f, i) => (
            <div key={`${f.label}-${i}`} className="contents">
              <dt className="text-fg-muted">{f.label}</dt>
              {/* Plain text: ERPNext's own values, never markup. */}
              <dd className="min-w-0 whitespace-pre-wrap break-words text-fg">{f.value}</dd>
            </div>
          ))}
        </dl>
        {card.lines.length > 0 && (
          <table className="mt-2 w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-fg-muted">
                <th className="font-medium">Item</th>
                <th className="font-medium text-right">Qty</th>
                <th className="font-medium pl-3">Warehouse</th>
              </tr>
            </thead>
            <tbody>
              {card.lines.map((line, i) => (
                <tr key={i} className="align-top">
                  <td className="break-words text-fg">{line.item_code}</td>
                  <td className="text-right tabular-nums text-fg">{line.qty}</td>
                  <td className="pl-3 break-words text-fg">{line.warehouse}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {card.notes.map((note, i) => (
          <p key={i} className="mt-2 text-sm text-fg-muted break-words">{note}</p>
        ))}
      </div>

      {done ? (
        <div className="mt-4 space-y-2">
          <p className="text-sm text-emerald-700 dark:text-emerald-300">
            {created
              ? `Draft ${created.name} ${created.already_created ? "was already" : "is now"} in ERPNext, unsubmitted. Review and submit it there.`
              : "The draft is in ERPNext, unsubmitted. Review and submit it there."}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            {link && (
              <a
                href={link}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm font-medium text-accent underline"
              >
                Open in ERPNext
              </a>
            )}
            <Button variant="secondary" onClick={() => onGone(card.decision_id)}>
              Close
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button variant="primary" onClick={() => void approve()} disabled={busy !== null}>
            {busy ?? (card.last_error ? "Try again" : "Approve")}
          </Button>
          <Button variant="ghost" className="ml-auto" onClick={() => void dismiss()} disabled={busy !== null}>
            Dismiss
          </Button>
        </div>
      )}
      {error && <p className="mt-2 text-sm text-red-500 break-words">{error}</p>}
    </article>
  );
}

// The chat: the draft cards a reply left (its propose_erpnext_draft chips
// carry their ids), shown under it while they wait.
export function ChatErpnextDraftCards({ ids }: { ids: number[] }) {
  const { cards, gone } = useErpnextDraftCards();
  const shown = cards.filter((c) => ids.includes(c.decision_id));
  if (shown.length === 0) return null;
  return (
    <div className="mt-3 space-y-3">
      {shown.map((card) => (
        <ErpnextDraftCardItem key={card.decision_id} card={card} onGone={gone} />
      ))}
    </div>
  );
}
