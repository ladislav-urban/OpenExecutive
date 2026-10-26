"use client";

import { useEffect, useSyncExternalStore } from "react";

import { useEmbed } from "@/components/embed/EmbedProvider";
import Button from "@/components/ui/Button";
import {
  approveErpnextDraft,
  dismissErpnextDraft,
  getErpnextDraftCards,
  type ErpnextDraftCard,
  type ErpnextDraftCreated,
} from "@/lib/api";
import { openMessage } from "@/lib/embed";
import { draftMadeLine } from "@/lib/erpnextDraftWords";
import { erpnextLink } from "@/lib/erpnextLinks";
import { formatRelativeTime } from "@/lib/relativeTime";

// Home and chat: ERPNext drafts the Executive prepared at your request (a
// ToDo, a Material Request, a Quotation, a Purchase Order), shown as cards
// in "Needs you". Each card shows exactly what approval will write; ERPNext's text on it is shown as plain
// text and never as markup. Approve makes it once, unsubmitted, as your own
// ERPNext user; you review and submit it in ERPNext. GET /erpnext/drafts
// answers only the card's own person, so nothing shows for anyone else or on
// a backend without it.
//
// One store for every view of the cards (Today and each chat reply that left
// one): one request however many are on screen, and a card approved,
// dismissed or closed in one view is the same everywhere.

interface DraftCardsState {
  cards: ErpnextDraftCard[];
  // What's in progress on a card, by its id.
  busy: Record<number, string>;
  // Approved here: what it made (null when the reply didn't say).
  made: Record<number, ErpnextDraftCreated | null>;
  // Why the last tap here failed.
  errors: Record<number, string>;
  // Failed here and no longer waiting (closed after its last try, or handled
  // elsewhere): shown with its error until closed, without Approve.
  closed: Record<number, true>;
  // The list has been asked for at least once (answered or not).
  loaded: boolean;
}

const EMPTY: DraftCardsState = { cards: [], busy: {}, made: {}, errors: {}, closed: {}, loaded: false };
let state: DraftCardsState = EMPTY;
const listeners = new Set<() => void>();
let loading: Promise<void> | null = null;

function update(change: (s: DraftCardsState) => DraftCardsState) {
  state = change(state);
  for (const listener of listeners) listener();
}

function without<T>(record: Record<number, T>, id: number): Record<number, T> {
  const rest = { ...record };
  delete rest[id];
  return rest;
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

const read = () => state;
const readOnServer = () => EMPTY;

function load(): Promise<void> {
  if (!loading) {
    loading = getErpnextDraftCards()
      .then((fresh) =>
        update((s) => {
          const waiting = new Set(fresh.map((c) => c.decision_id));
          // A card approved or refused here stays on screen with its outcome,
          // though it no longer waits.
          const kept = s.cards.filter(
            (c) => !waiting.has(c.decision_id) && (c.decision_id in s.made || c.decision_id in s.errors),
          );
          const closed = { ...s.closed };
          for (const c of kept) if (!(c.decision_id in s.made)) closed[c.decision_id] = true;
          return { ...s, cards: [...fresh, ...kept], closed };
        }),
      )
      .catch(() => { /* no card is better than a broken one */ })
      .finally(() => {
        loading = null;
        if (!state.loaded) update((s) => ({ ...s, loaded: true }));
      });
  }
  return loading;
}

// Load the cards again, as after a card was left from outside this store
// (an alert's "Prepare" button).
export const reloadErpnextDraftCards = load;

function gone(id: number) {
  update((s) => ({
    ...s,
    cards: s.cards.filter((c) => c.decision_id !== id),
    busy: without(s.busy, id),
    made: without(s.made, id),
    errors: without(s.errors, id),
    closed: without(s.closed, id),
  }));
}

async function act(id: number, doing: string, run: () => Promise<void>, failed: string) {
  if (state.busy[id]) return;
  update((s) => ({ ...s, busy: { ...s.busy, [id]: doing }, errors: without(s.errors, id) }));
  try {
    await run();
  } catch (err) {
    const message = err instanceof Error ? err.message : failed;
    update((s) => ({ ...s, errors: { ...s.errors, [id]: message } }));
    // Closed after its last try, or handled in another view: every view
    // learns it from the list.
    void load();
  } finally {
    update((s) => ({ ...s, busy: without(s.busy, id) }));
  }
}

const approveCard = (id: number) =>
  act(id, "Creating the draft…", async () => {
    const created = await approveErpnextDraft(id);
    update((s) => ({ ...s, made: { ...s.made, [id]: created } }));
  }, "Couldn't make the draft.");

const dismissCard = (id: number) =>
  act(id, "Dismissing…", async () => {
    await dismissErpnextDraft(id);
    gone(id);
  }, "Couldn't dismiss that draft.");

export function useErpnextDraftCards() {
  const s = useSyncExternalStore(subscribe, read, readOnServer);
  useEffect(() => {
    void load();
  }, []);
  return { cards: s.cards, loaded: s.loaded, gone };
}

// What the draft cards are, for the Needs you header's tip.
export const ERPNEXT_DRAFTS_TIP =
  "ERPNext drafts you asked the Executive to prepare. Each shows exactly what will be written, " +
  "and ERPNext has already checked it. Nothing is created until you tap Approve, and then only an unsubmitted " +
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
  const s = useSyncExternalStore(subscribe, read, readOnServer);
  const { hostOrigin } = useEmbed();
  const id = card.decision_id;
  const busy = s.busy[id] ?? null;
  const done = id in s.made;
  const created = s.made[id] ?? null;
  const closed = !done && id in s.closed;
  const error = s.errors[id] ?? (card.last_error && !done ? `Last try: ${card.last_error}` : null);
  const when = formatRelativeTime(card.created_at);

  const link = created?.url && /^https?:\/\//.test(created.url) ? created.url : null;
  // In a host's frame, a document on the host's own origin opens in its desk.
  const opens = link ? erpnextLink(link, hostOrigin) : null;
  const openInHost = (event: React.MouseEvent<HTMLAnchorElement>) => {
    // A click asking for a new tab or window gets one.
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (opens?.kind !== "host" || !hostOrigin) return;
    const message = openMessage({ kind: "host", route: opens.route });
    if (!message) return;
    event.preventDefault();
    window.parent.postMessage(message, hostOrigin);
  };
  // A Quotation's or Purchase Order's lines carry ERPNext's rates; a
  // Quotation's may name no warehouse.
  const priced = card.lines.some((line) => Boolean(line.rate || line.amount));
  const anyWarehouse = card.lines.some((line) => Boolean(line.warehouse));
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
      {card.asked_in === "erpnext_panel" && (
        // ERPNext's word named you here, not a sign-in: said before its
        // headline, which whoever asked wrote.
        <div className="mb-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-sm text-amber-800 dark:text-amber-200">
          Asked for in ERPNext&apos;s panel. Approve it only if you asked for it there.
        </div>
      )}
      {card.why && (
        <div className="text-base sm:text-[17px] font-semibold leading-snug text-fg break-words">{card.why}</div>
      )}

      <div className="mt-3 rounded-xl border border-line bg-surface px-3 py-2">
        <span className="block text-xs font-semibold uppercase tracking-wide text-fg-muted">
          What will be written
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
                {priced && <th className="font-medium text-right pl-3">Rate</th>}
                {priced && <th className="font-medium text-right pl-3">Amount</th>}
                {anyWarehouse && <th className="font-medium pl-3">Warehouse</th>}
              </tr>
            </thead>
            <tbody>
              {card.lines.map((line, i) => (
                <tr key={i} className="align-top">
                  <td className="break-words text-fg">{line.item_code}</td>
                  <td className="text-right tabular-nums text-fg">{line.qty}</td>
                  {priced && <td className="pl-3 text-right tabular-nums text-fg">{line.rate || "—"}</td>}
                  {priced && <td className="pl-3 text-right tabular-nums text-fg">{line.amount || "—"}</td>}
                  {anyWarehouse && <td className="pl-3 break-words text-fg">{line.warehouse}</td>}
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
            {draftMadeLine(card.doctype, created?.name ?? null, created?.already_created ?? false)}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            {link && (
              <a
                href={link}
                target="_blank"
                rel="noopener noreferrer"
                onClick={openInHost}
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
      ) : closed ? (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button variant="secondary" onClick={() => onGone(id)}>
            Close
          </Button>
        </div>
      ) : (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button variant="primary" onClick={() => void approveCard(id)} disabled={busy !== null}>
            {busy ?? (card.last_error || s.errors[id] ? "Try again" : "Approve")}
          </Button>
          <Button variant="ghost" className="ml-auto" onClick={() => void dismissCard(id)} disabled={busy !== null}>
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
