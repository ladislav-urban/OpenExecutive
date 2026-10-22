"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import ApprovedRow from "@/components/briefing/ApprovedRow";
import ProposalCard from "@/components/briefing/ProposalCard";
import { usePageCrumb } from "@/components/shell/PageCrumb";
import {
  ackAlert,
  approveAlert,
  approveDecision,
  getAlertCard,
  rejectDecision,
  rerunWorkflowRun,
  snoozeAlert,
  type AlertCard,
  type ProposalItem,
} from "@/lib/api";
import type { AlertApproval } from "@/lib/approvals";

// One of Home's proposal cards on a page of its own, by its alert id, as a
// decision's is at /cards/<id>: what a host's frame opens to have it
// approved (in a frame each action asks in a window of its own,
// lib/cardActions.ts), and a link anyone can keep. It shows the card with
// Home's component and the actions GET /alerts/{id}/card gives it; the
// server answers only the card's own person (the principal, or whoever it
// is routed to), so anyone else sees that it isn't there.
export default function AlertCardPage() {
  const params = useParams<{ id: string }>();
  const id = /^\d+$/.test(params?.id ?? "") ? Number(params.id) : null;
  usePageCrumb(id !== null ? `Card ${id}` : "Card");
  // undefined while loading, null when it isn't the caller's or isn't waiting.
  const [card, setCard] = useState<AlertCard | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [approval, setApproval] = useState<AlertApproval | null>(null);

  useEffect(() => {
    if (id === null) return;
    const controller = new AbortController();
    getAlertCard(id, controller.signal)
      .then(setCard)
      .catch(() => setCard(null));
    return () => controller.abort();
  }, [id]);

  // Run one action; on success the card leaves the page with a line saying
  // what happened, on failure it stays with the reason.
  const act = useCallback(async (run: () => Promise<string>) => {
    setBusy(true);
    setFailed(null);
    try {
      setDone(await run());
    } catch (e) {
      setFailed(e instanceof Error ? e.message : "That didn't work. Try again.");
    } finally {
      setBusy(false);
    }
  }, []);

  const onApprove = (p: ProposalItem) =>
    void act(async () => {
      if (p.decision_instance_id != null) {
        await approveDecision(p.decision_instance_id);
        return "Approved.";
      }
      setApproval(await approveAlert(p.alert_id));
      return "Approved.";
    });
  const onApproveWithEdits = (p: ProposalItem, editedBody: string) =>
    void act(async () => {
      setApproval(await approveAlert(p.alert_id, editedBody));
      return "Approved with your edits.";
    });
  const onDismiss = (p: ProposalItem) =>
    void act(async () => {
      // A card of another kind was answered on its own card already.
      if ((p.card_kind ?? "proposal") !== "proposal") return "Done.";
      if (p.decision_instance_id != null) await rejectDecision(p.decision_instance_id);
      else await ackAlert(p.alert_id, "dismissed");
      return "Dismissed.";
    });
  const onSnooze = (p: ProposalItem, choice: { short: string; until: string }) =>
    void act(async () => {
      await snoozeAlert(p.alert_id, choice.until);
      return `Snoozed until ${choice.short}.`;
    });
  const onRerun = (p: ProposalItem) =>
    void act(async () => {
      if (!p.failed_run_id) return "Done.";
      await rerunWorkflowRun(p.failed_run_id);
      return "Running it again now. The result goes where it normally would.";
    });

  return (
    <main className="mx-auto w-full max-w-2xl px-4 sm:px-6 py-6 space-y-4">
      {id === null || card === null ? (
        <p className="text-sm text-fg-muted">
          This card isn&rsquo;t here: it may be someone else&rsquo;s, or no longer waiting.
        </p>
      ) : card === undefined ? (
        <p className="text-sm text-fg-muted">Loading the card…</p>
      ) : done ? (
        <>
          <p className="text-sm text-fg" role="status">
            {done}
          </p>
          {approval && (
            <ApprovedRow
              approval={approval}
              headline={card.proposal.headline}
              onRetry={() =>
                void act(async () => {
                  setApproval(await approveAlert(approval.alert_id, approval.edited_body ?? undefined));
                  return "Approved.";
                })
              }
              onClose={() => setApproval(null)}
            />
          )}
        </>
      ) : (
        <ProposalCard
          proposal={card.proposal}
          people={card.people}
          onApprove={onApprove}
          onApproveWithEdits={onApproveWithEdits}
          onDismiss={onDismiss}
          onSnooze={onSnooze}
          onRerun={onRerun}
          busy={busy}
          defaultBodyExpanded
          emphasized
        />
      )}
      {failed && (
        <p className="text-sm text-rose-700 dark:text-rose-300" role="alert">
          {failed}
        </p>
      )}
      <p className="text-sm">
        <Link href="/" className="text-accent hover:underline">
          Everything waiting for you
        </Link>
      </p>
    </main>
  );
}
