"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  ackAlert,
  answerAlert,
  approveAlert,
  approveDecision,
  bulkAckAlerts,
  dismissActionCard,
  dismissErpnextDraft,
  dismissInviteCard,
  dismissReplyCard,
  dismissRightNowRow,
  getAlertApproval,
  getDelegation,
  getToday,
  rejectDecision,
  reopenAlert,
  rerunWorkflowRun,
  reviewAlerts,
  snoozeAlert,
  type HomeTile,
  type DoneRow,
  type ProposalItem,
  type SummaryPart,
  type RightNowRow,
  type Today,
} from "@/lib/api";
import type { BriefingPanel } from "@/lib/chatRoutes";
import { greeting } from "@/lib/briefingSummary";
import { FIRST_RUN_LINKS } from "@/lib/firstRun";
import { principalIdOf } from "@/lib/dueSoon";
import {
  NEEDS_YOU_OLDER_AFTER_DAYS,
  ageAnchor,
  clearAll,
  clearNotice,
  groupByRequest,
  newestAnchor,
  splitOlder,
} from "@/lib/needsYou";
import { BRIEFING_REFRESH_INTERVAL_MS, shouldRefreshOnFocus } from "@/lib/narrativeFreshness";
import { reviewRanLabel } from "@/lib/rhythmCards";
import InfoTip from "./InfoTip";
import { ACTION_CARDS_TIP, ActionCardItem, useActionCards } from "./ActionCards";
import { ErpnextDraftCardItem, useErpnextDraftCards, useErpnextDraftsTip } from "./ErpnextDraftCards";
import { ProposedRuleItem, useProposedRules } from "./ProposedRules";
import { INVITE_CARDS_TIP, InviteCardItem, useInviteCards } from "./InviteCards";
import { REPLIES_WAITING_TIP, ReplyCardItem, useReplyCards } from "./RepliesWaiting";
import Icon from "./Icon";
import {
  HandledPanelBody,
  InFlightPanelBody,
  MonitoringPanelBody,
  PracticeClientsPanelBody,
} from "./briefing/AwarenessPanels";
import DoneForYou, { DONE_FOR_YOU_ID } from "./briefing/DoneForYou";
import { anyWorking } from "@/lib/doneForYou";
import FirstWeekCard from "./briefing/FirstWeekCard";
import { RightNowItem } from "./briefing/RightNow";
import ProposalCard from "./briefing/ProposalCard";
import RequestGroupCard from "./briefing/RequestGroupCard";
import { TONE_TEXT, type ContinueHandler } from "./briefing/shared";
import {
  DueSoonPanelBody,
  ProjectsPanelBody,
  WeeklyReviewPanelBody,
  useActiveProjects,
  useOpenLoops,
  useWeeklyReview,
} from "./briefing/SoloPanels";
import { DepartmentsPanelBody, PeoplePanelBody } from "./briefing/TeamPanels";
import { buttonClass } from "./ui/Button";
import OverflowMenu from "./ui/OverflowMenu";
import SidePanel from "./ui/SidePanel";
import { useWorkspace } from "./workspace/WorkspaceContext";

// The card pieces stay importable from here for pages that show them alone.
export { ProposalCard };
export { DeptCard } from "./briefing/TeamPanels";

// Home, the briefing: a greeting and one summary line, the Ask box, the
// "Your decision" queue as large cards (Right now's rows, replies, invites,
// drafts and proposals, a request's approvals grouped as one card, old ones
// folded into "Older"), Done for you, then one quiet line of counts, each
// opening its full list in a side panel.
//
// GET /today works out Home's rules for the caller (briefing/home_rules.py):
// each card's lane, kind and actions, Needs you's count and older-than
// offer, the summary line, the tiles, the quiet day, the handled rows and
// the snooze times. This page shows them and keeps only what is its own:
// cards hidden at once on a click until the next fetch, panels, notices.

// The panels Home builds from /today, in the order its side panels render.
const HOME_PANEL_KEYS: readonly BriefingPanel[] = [
  "brief",
  "departments",
  "people",
  "team",
  "inFlight",
  "handled",
  "monitoring",
  "clients",
];

// "Needs you" shows this many cards before a "Show N more" toggle.
const NEEDS_YOU_VISIBLE = 3;
// How often Home re-reads while something in Done for you is running, and
// for how long.
const DONE_POLL_MS = 8000;
const DONE_POLL_MAX_MS = 30 * 60 * 1000;

const NEEDS_YOU_ID = "sec-needs-you";

type PanelKey =
  | "departments"
  | "people"
  | "inFlight"
  | "handled"
  | "monitoring"
  | "team"
  | "clients"
  | "projects"
  | "dueSoon"
  | "weeklyReview";

// A count tile: one of GET /today's, or solo's own (due soon, projects, the
// weekly review), which come from their own endpoints.
interface Tile {
  key: PanelKey;
  value: string;
  label: string;
  sub: string;
  subTone?: keyof typeof TONE_TEXT;
}

const fromToday = (t: HomeTile): Tile => ({
  key: t.key,
  value: t.value,
  label: t.label,
  sub: t.sub,
  subTone: t.tone ?? undefined,
});

interface BriefingProps {
  // Called when the user clicks a briefing item to continue the thread
  // in chat. The parent (the root page) switches its main view from
  // briefing → chat and seeds the input with `prompt`. When omitted, items
  // render as plain navigation links.
  onContinue?: ContinueHandler;
  // Opens one of the reader's chats in place (Done for you's See / Watch).
  onOpenChat?: (sessionId: string, watch?: boolean) => void;
  // Set to true when this Briefing is the root landing surface — adds the
  // greeting header.
  showHeader?: boolean;
  // Personalises the greeting when showHeader is true.
  firstName?: string;
  // Home's one banner slot: the most important notice the page has (paused,
  // no profile, a brief that wasn't sent). When empty, the quiet-day note
  // takes the slot.
  banner?: React.ReactNode;
  // Show one of Home's panels as a page of its own (/briefing/<panel>)
  // instead of Home: the same body and footer links its side panel has.
  panel?: BriefingPanel;
}

// A panel of Home: what its side panel, or its page, shows.
interface PanelView {
  title: string;
  subtitle?: React.ReactNode;
  width?: "lg";
  footer?: React.ReactNode;
  body: React.ReactNode;
}

export default function Briefing({ onContinue, onOpenChat, showHeader = false, firstName, banner, panel }: BriefingProps) {
  const router = useRouter();
  // Solo (one person, just for themselves): no departments, roster or
  // cross-team lanes — see the `solo` branches below.
  const { mode } = useWorkspace();
  const solo = mode === "solo";
  const [today, setToday] = useState<Today | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Alerts the user has acted on this session. We optimistically drop
  // them from the rendered lists so the click feels instant; the canonical
  // state is picked up by the next /today fetch.
  const [actedAlertIds, setActedAlertIds] = useState<Set<number>>(new Set());
  // "Needs you" shows NEEDS_YOU_VISIBLE cards; the rest sit behind one toggle.
  const [showAllNeedsYou, setShowAllNeedsYou] = useState(false);
  // Cards untouched for a few days fold into one "Older (N)" line.
  const [showOlder, setShowOlder] = useState(false);
  const [clearingOlder, setClearingOlder] = useState(false);
  const [olderNotice, setOlderNotice] = useState<string | null>(null);
  // "Re-check relevance" runs the Executive's review on demand.
  const [recheckBusy, setRecheckBusy] = useState(false);
  // The card just snoozed, for the "Snoozed until …" notice and its Undo.
  const [snoozed, setSnoozed] = useState<{ alertId: number; short: string } | null>(null);
  // Handled-rail rows undone this session (keyed per row, see handledKey).
  const [undoneRows, setUndoneRows] = useState<Set<string>>(new Set());
  // Which tile's side panel (or the brief) is open.
  const [openPanel, setOpenPanel] = useState<PanelKey | null>(null);
  const [askText, setAskText] = useState("");

  // Re-pull /today after a server-side mutation (e.g. a decision approve/reject)
  // so derived data — the narrative, per-person and department counts —
  // re-syncs. The optimistic actedAlertIds set already hides the card; this
  // refreshes everything computed from it. Best-effort: a failed refresh leaves
  // the stale-but-still-usable view rather than erroring the briefing.
  const [mountedAt] = useState(() => Date.now());
  const lastFetchRef = useRef(mountedAt);
  const refreshToday = useCallback(() => {
    lastFetchRef.current = Date.now();
    getToday().then(setToday).catch(() => { /* keep current view on failure */ });
  }, []);

  // Keep Home current when the tab comes back, and every few minutes while
  // it is open.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible" && shouldRefreshOnFocus(lastFetchRef.current, Date.now())) {
        refreshToday();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    const interval = setInterval(() => {
      if (document.visibilityState === "visible") refreshToday();
    }, BRIEFING_REFRESH_INTERVAL_MS);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      clearInterval(interval);
    };
  }, [refreshToday]);

  // Approve carries the card out on the server. Decision-backed cards
  // (gated calendar bookings, held actions) go through /decisions, which
  // books or sends and clears the companion alert. Every other proposal goes
  // through /alerts/{id}/approve, which records who approved it, then queues
  // the action it held or runs the work as a chat on the server; a row in
  // Done for you follows that work until it is done.
  const handleApprove = useCallback(async (proposal: ProposalItem) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      if (proposal.decision_instance_id != null) {
        await approveDecision(proposal.decision_instance_id);
        refreshToday();  // re-sync narrative + counts (no chat nav to trigger it)
        return;
      }
      // Its progress shows in Done for you, read from the server.
      await approveAlert(proposal.alert_id);
      refreshToday();
    } catch (e) {
      // Revert the optimistic removal on failure so the user can retry.
      setActedAlertIds(prev);
      console.error("Approve failed", e);
    }
  }, [actedAlertIds, refreshToday]);

  // A card that asks something is answered on the card; the
  // server closes it and the Executive carries on in a new chat (its
  // progress shows in Done for you). Throws so the card can say why.
  const handleAnswer = useCallback(async (proposal: ProposalItem, answer: string) => {
    await answerAlert(proposal.alert_id, answer);
    setActedAlertIds((prev) => new Set(prev).add(proposal.alert_id));
    refreshToday();
  }, [refreshToday]);

  // Dismiss is a record-and-forget decision: the card disappears
  // immediately, the backend marks the alert ``dismissed`` (so the
  // Executive stops re-surfacing the same thread), and the user stays on
  // the briefing — no chat turn is seeded. On failure we revert the
  // optimistic removal so the user can see the card came back and retry.
  const handleDismiss = useCallback(async (proposal: ProposalItem) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      // A roster request, a Take the lead training card or a held chat
      // action was already answered on its own card (which also clears the
      // companion alert): only hide it and re-sync.
      if ((proposal.card_kind ?? "proposal") !== "proposal") {
        refreshToday();
        return;
      }
      // Decision-backed cards reject server-side (which also clears the
      // companion alert); ordinary alerts just get acked "dismissed".
      if (proposal.decision_instance_id != null) {
        await rejectDecision(proposal.decision_instance_id);
        refreshToday();  // re-sync narrative + counts (no chat nav to trigger it)
        return;
      }
      await ackAlert(proposal.alert_id, "dismissed");
      refreshToday();  // re-sync the counts and the summary line
    } catch (e) {
      setActedAlertIds(prev);
      console.error("Dismiss failed", e);
    }
  }, [actedAlertIds, refreshToday]);

  // Snooze: the card leaves now and the server brings it back at `until`.
  // Undo clears the snooze and shows the card again.
  const handleSnooze = useCallback(async (proposal: ProposalItem, choice: { short: string; until: string }) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      await snoozeAlert(proposal.alert_id, choice.until);
      setSnoozed({ alertId: proposal.alert_id, short: choice.short });
      refreshToday();  // re-sync the counts and the summary line
    } catch (e) {
      setActedAlertIds(prev);
      console.error("Snooze failed", e);
    }
  }, [actedAlertIds, refreshToday]);

  const handleUnsnooze = useCallback(async () => {
    if (!snoozed) return;
    try {
      await snoozeAlert(snoozed.alertId, null);
      setActedAlertIds((prev) => {
        const next = new Set(prev);
        next.delete(snoozed.alertId);
        return next;
      });
      setSnoozed(null);
      refreshToday();
    } catch (e) {
      console.error("Undo snooze failed", e);
    }
  }, [snoozed, refreshToday]);

  // Run again on a failed scheduled run: the server queues it and closes the
  // card, so the card leaves at once and comes back only if it fails again.
  const [rerunNotice, setRerunNotice] = useState<string | null>(null);
  const handleRerun = useCallback(async (proposal: ProposalItem) => {
    if (!proposal.failed_run_id) return;
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      await rerunWorkflowRun(proposal.failed_run_id);
      setRerunNotice("Running it again now. The result goes where it normally would.");
      refreshToday();
    } catch (e) {
      setActedAlertIds(prev);
      setRerunNotice(e instanceof Error ? e.message : "Couldn't run it again.");
    }
  }, [actedAlertIds, refreshToday]);

  // Bulk dismiss: sends explicit ids (the cards the caller can see), never a
  // server-side age sweep. Same optimistic-removal + rollback pattern as the
  // single-card handlers.
  const handleBulkDismiss = useCallback(async (ids: number[]) => {
    if (ids.length === 0) return;
    const prev = actedAlertIds;
    const next = new Set(prev);
    ids.forEach((id) => next.add(id));
    setActedAlertIds(next);
    try {
      await bulkAckAlerts({ status: "dismissed", alert_ids: ids });
      refreshToday();
    } catch (e) {
      setActedAlertIds(prev);
      console.error("Bulk dismiss failed", e);
    }
  }, [actedAlertIds, refreshToday]);

  // Re-check relevance: ask the Executive to review every open alert now
  // (route / escalate / draft / merge / resolve within authority), then
  // re-pull /today so the verdicts, chips and "handled" rail refresh.
  const handleRecheck = useCallback(async () => {
    setRecheckBusy(true);
    try {
      await reviewAlerts();
      refreshToday();
    } catch (e) {
      console.error("Alert review failed", e);
    } finally {
      setRecheckBusy(false);
    }
  }, [refreshToday]);

  // Undo an autonomous close from the Handled panel. The rail is rebuilt
  // from the audit log on every fetch (the "closed" row persists after a
  // reopen), so a 409 "already open" after a reload counts as done.
  const handleReopen = useCallback(async (rowKey: string, alertId: number) => {
    try {
      await reopenAlert(alertId);
      setUndoneRows((prev) => new Set(prev).add(rowKey));
      refreshToday();
    } catch (e) {
      if (e instanceof Error && /409|already/i.test(e.message)) {
        setUndoneRows((prev) => new Set(prev).add(rowKey));
        return;
      }
      console.error("Reopen failed", e);
    }
  }, [refreshToday]);

  // Approve-with-edits: the user tweaked the draft and wants exactly what
  // they wrote sent (no rephrasing). Same server-side approval as Approve,
  // with the edited text carried verbatim.
  const handleApproveWithEdits = useCallback(async (proposal: ProposalItem, editedBody: string) => {
    const prev = actedAlertIds;
    setActedAlertIds(new Set(prev).add(proposal.alert_id));
    try {
      await approveAlert(proposal.alert_id, editedBody);
      refreshToday();
    } catch (e) {
      setActedAlertIds(prev);
      console.error("Approve-with-edits failed", e);
    }
  }, [actedAlertIds, refreshToday]);

  // Try again on an approval that didn't work (Done for you): the server
  // lets a card whose last approval failed be approved again, with the same
  // edits, and the row turns back to Working on it.
  const handleRetryApproval = useCallback(async (row: DoneRow) => {
    if (row.alert_id == null) return;
    try {
      const last = row.approval_id != null ? await getAlertApproval(row.approval_id) : null;
      await approveAlert(row.alert_id, last?.edited_body ?? undefined);
      refreshToday();
    } catch (e) {
      console.error("Retry approval failed", e);
    }
  }, [refreshToday]);

  useEffect(() => {
    let cancelled = false;
    getToday()
      .then((t) => { if (!cancelled) setToday(t); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  // Replies the Executive drafted in the owner's own mailbox (Act as me):
  // cards in Needs you. Empty for everyone else.
  const replies = useReplyCards();
  // Actions it suggested from the caller's email, to approve (Act as me).
  const actionCards = useActionCards();
  // ERPNext drafts it prepared at the caller's request, to approve.
  const draftCards = useErpnextDraftCards();
  const draftsTip = useErpnextDraftsTip();
  // Bank rules the bank-rules job proposed, waiting for a System Manager in
  // the desk (the principal's own, read as them). Empty for everyone else.
  const proposedRules = useProposedRules();
  // Meeting invites from people the caller knows, to accept or decline (Act as me).
  const inviteCards = useInviteCards();
  // Solo's own sections load on their own, so their tiles can count them.
  const principalId = solo ? principalIdOf(today?.people ?? []) : null;
  const projects = useActiveProjects(solo);
  const openLoops = useOpenLoops(principalId);
  const weeklyReview = useWeeklyReview(solo);

  // Hand an Ask-box question to chat as the user's own words.
  const submitAsk = () => {
    const text = askText.trim();
    if (!text || !onContinue) return;
    onContinue(text);
  };

  // Jump from the Handled panel to the alert's card; when it is folded
  // behind "Show more" open the queue first, and land on the queue when the
  // card isn't on the page at all.
  const jumpToAlert = (alertId: number) => {
    // On a panel's own page there is no queue to scroll to: the card's page.
    if (panel) {
      router.push(`/cards/alert/${alertId}`);
      return;
    }
    setOpenPanel(null);
    setShowAllNeedsYou(true);
    setTimeout(() => {
      const target = document.getElementById(`alert-${alertId}`) ?? document.getElementById(NEEDS_YOU_ID);
      target?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 120);
  };

  // What Right now's rows open: the email (and its reply card, shared with
  // Needs you). ✕ on a row clears it at once; the server keeps it out until
  // something newer lands on it.
  const onDismissRow = useCallback((row: RightNowRow) => {
    setToday((t) => t && { ...t, right_now: (t.right_now ?? []).filter((r) => r.key !== row.key) });
    dismissRightNowRow(row.key)
      .then((kept) => {
        if (!kept) return getToday().then(setToday);
      })
      .catch((e) => console.error("Dismissing a Right now row failed", e));
  }, []);
  const rightNowActions = { replies, onContinue, onDismiss: onDismissRow };

  const dateLabel = new Date().toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });

  const departments = today?.departments ?? [];

  // Each card's lane comes from /today (`lane`): the caller's own, a
  // teammate's, or monitoring. Cards acted on here leave at once, before
  // the next fetch re-syncs the counts.
  const liveProposals = (today?.proposals ?? []).filter((p) => !actedAlertIds.has(p.alert_id));
  const monitoringProposals = liveProposals.filter((p) => p.lane === "monitoring");
  const mineProposals = liveProposals.filter((p) => p.lane === "needs_you");
  const otherProposals = liveProposals.filter((p) => p.lane === "team");
  const dismissOlder = today?.dismiss_older ?? null;
  const staleNeedsYouIds = (dismissOlder?.alert_ids ?? []).filter((id) => !actedAlertIds.has(id));
  const handledRows = today?.handled_rows ?? [];
  const inFlight = today?.in_flight ?? [];
  const practiceClients = today?.practice_clients ?? [];
  const needsYouCount = today?.needs_you_count ?? 0;
  const summary: SummaryPart[] = today?.summary ?? [];
  const isQuiet = today?.quiet ?? false;
  // Waiting on the owner with no card (Right now's rows): listed in Needs you.
  const rightNowRows = today?.right_now ?? [];
  // What the Executive did or is doing for the viewer today.
  const doneRows = today?.done_for_you ?? [];
  // While a row is Working on it, read again every few seconds so it turns
  // to Done in place; the work goes on whether or not the page is open.
  const working = anyWorking(doneRows);
  useEffect(() => {
    if (!working) return;
    const started = Date.now();
    const timer = setInterval(() => {
      if (Date.now() - started > DONE_POLL_MAX_MS) clearInterval(timer);
      else if (document.visibilityState === "visible") refreshToday();
    }, DONE_POLL_MS);
    return () => clearInterval(timer);
  }, [working, refreshToday]);

  // The count tiles — only those with something to show: solo's own first,
  // then /today's.
  const tiles: Tile[] = [];
  if (today) {
    if (solo) {
      const view = openLoops.view;
      if (principalId != null && (openLoops.error || (view && (view.items.length > 0 || view.later > 0)))) {
        const overdue = view?.items.filter((i) => i.overdue).length ?? 0;
        tiles.push({
          key: "dueSoon",
          value: String(view?.items.length ?? "–"),
          label: "Due soon",
          sub: overdue > 0 ? `${overdue} overdue` : view && view.items.length > 0 ? "this week" : `${view?.later ?? 0} later`,
          subTone: overdue > 0 ? "amber" : undefined,
        });
      }
      if (projects.error || (projects.projects && projects.projects.length > 0)) {
        tiles.push({
          key: "projects",
          value: String(projects.projects?.length ?? "–"),
          label: "Your projects",
          sub: "active",
        });
      }
      if (weeklyReview) {
        tiles.push({
          key: "weeklyReview",
          value: "Ready",
          label: "This week's review",
          sub: reviewRanLabel(weeklyReview.completed_at) || weeklyReview.period || "",
        });
      }
    }
    tiles.push(...(today.tiles ?? []).map(fromToday));
  }

  // The Needs you queue: replies waiting first (they came in first, as their
  // own section did), then the proposals in score order, with the approvals
  // one request held together shown as one card. Each item knows how old it
  // is and how Clear all closes it: a card untouched for a few days folds
  // into the "Older (N)" line (lib/needsYou.ts).
  type QueueItem = {
    key: string;
    // Age anchor (ISO): when it came, or last changed.
    at: string;
    // Never folds: a card answered only on itself, or one being sent.
    keep?: boolean;
    // Closes it the way its own Skip would, and drops it from the page.
    clear: () => Promise<void>;
    render: (emphasized: boolean) => React.ReactNode;
  };
  const hideAlerts = (ids: number[]) =>
    setActedAlertIds((prev) => {
      const next = new Set(prev);
      ids.forEach((id) => next.add(id));
      return next;
    });
  const clearProposal = async (p: ProposalItem) => {
    // Same reject as Skip, but one already answered elsewhere (404/409)
    // counts as cleared.
    if (p.decision_instance_id != null) await dismissActionCard(p.decision_instance_id);
    else await ackAlert(p.alert_id, "dismissed");
    hideAlerts([p.alert_id]);
  };
  const queue: QueueItem[] = [
    // What's waiting that has no card (mail it couldn't draft, unsent
    // drafts, chat asks): one row each, first, since it came in first.
    ...rightNowRows.map((row) => ({
      key: `rn-${row.key}`,
      at: row.at,
      clear: async () => onDismissRow(row),
      render: () => <RightNowItem row={row} actions={rightNowActions} />,
    })),
    ...replies.cards.map((card) => ({
      key: `reply-${card.decision_id}`,
      at: card.created_at,
      keep: card.status === "executing",
      clear: async () => {
        await dismissReplyCard(card.decision_id);
        replies.gone(card.decision_id);
      },
      render: (emphasized: boolean) => (
        <ReplyCardItem
          card={card}
          onGone={replies.gone}
          onRefresh={replies.refresh}
          onChanged={replies.replace}
          emphasized={emphasized}
        />
      ),
    })),
    ...inviteCards.cards.map((card) => ({
      key: `invite-${card.decision_id}`,
      at: card.created_at,
      clear: async () => {
        await dismissInviteCard(card.decision_id);
        inviteCards.gone(card.decision_id);
      },
      render: (emphasized: boolean) => (
        <InviteCardItem card={card} onGone={inviteCards.gone} emphasized={emphasized} />
      ),
    })),
    ...actionCards.cards.map((card) => ({
      key: `actions-${card.decision_id}`,
      at: card.created_at,
      clear: async () => {
        await dismissActionCard(card.decision_id);
        actionCards.gone(card.decision_id);
      },
      render: (emphasized: boolean) => (
        <ActionCardItem card={card} onGone={actionCards.gone} emphasized={emphasized} />
      ),
    })),
    ...draftCards.cards.map((card) => ({
      key: `erpnext-draft-${card.decision_id}`,
      at: card.created_at,
      // Answered on its own card, by its own approver: never folded or
      // cleared with the rest.
      keep: true,
      clear: async () => {
        await dismissErpnextDraft(card.decision_id);
        draftCards.gone(card.decision_id);
      },
      render: (emphasized: boolean) => (
        <ErpnextDraftCardItem card={card} onGone={draftCards.gone} emphasized={emphasized} />
      ),
    })),
    ...proposedRules.map((rule) => ({
      key: `proposed-rule-${rule.name}`,
      at: rule.proposed,
      // Decided in the desk, by a System Manager: never folded or cleared here.
      keep: true,
      clear: async () => {},
      render: (emphasized: boolean) => <ProposedRuleItem rule={rule} emphasized={emphasized} />,
    })),
    ...groupByRequest(mineProposals).map((group): QueueItem => {
      if (group.length > 1) {
        return {
          key: `request-${group[0].group_key}`,
          at: newestAnchor(group),
          clear: async () => {
            const results = await Promise.allSettled(group.map(clearProposal));
            if (results.some((r) => r.status === "rejected")) throw new Error("Some didn't clear");
          },
          render: (emphasized: boolean) => (
            <RequestGroupCard
              proposals={group}
              emphasized={emphasized}
              onResolved={(done) => {
                hideAlerts(done.map((p) => p.alert_id));
                refreshToday();
              }}
            />
          ),
        };
      }
      const p = group[0];
      return {
        key: `alert-${p.alert_id}`,
        at: ageAnchor(p),
        // A roster request is answered on its card, never swept.
        keep: Boolean(p.roster_request),
        clear: () => clearProposal(p),
        render: (emphasized: boolean) => (
          <ProposalCard
            proposal={p}
            people={today?.people ?? []}
            onContinue={onContinue}
            onApprove={handleApprove}
            onDismiss={handleDismiss}
            onSnooze={handleSnooze}
            onRerun={handleRerun}
            onApproveWithEdits={handleApproveWithEdits}
            onAnswer={handleAnswer}
            defaultBodyExpanded={emphasized}
            emphasized={emphasized}
          />
        ),
      };
    }),
  ];
  const { current: currentQueue, older: olderQueue } = splitOlder(
    queue,
    (item) => item.at,
    new Date(),
    (item) => Boolean(item.keep),
  );
  const visibleQueue = showAllNeedsYou ? currentQueue : currentQueue.slice(0, NEEDS_YOU_VISIBLE);
  const hiddenCount = currentQueue.length - visibleQueue.length;
  const handleClearOlder = async () => {
    setClearingOlder(true);
    setOlderNotice(null);
    const { failed } = await clearAll(olderQueue);
    setClearingOlder(false);
    const notice = clearNotice(failed.length, olderQueue.length);
    setOlderNotice(notice);
    if (!notice) setShowOlder(false);
    refreshToday();
  };

  const needsYouMenu = [
    ...(dismissOlder && staleNeedsYouIds.length > 0
      ? [
          {
            label: dismissOlder.label,
            onSelect: () => void handleBulkDismiss(staleNeedsYouIds),
          },
        ]
      : []),
    {
      label: recheckBusy ? "Re-checking…" : "Re-check relevance",
      onSelect: () => void handleRecheck(),
      disabled: recheckBusy,
    },
  ];
  const showNeedsYouMenu = mineProposals.length > 0;

  // The first week: the first-week card takes the page's top slot, so the
  // day isn't called quiet and the summary line points at it.
  const firstWeekCard = today?.first_week_card ?? null;
  const firstWeek = Boolean(today?.first_week);
  // With nothing waiting, Needs you isn't shown: the summary line says
  // all clear. It stays in the first week, where its empty line points at
  // the mailbox step, and while one of its notices is up.
  const hideEmptyNeedsYou =
    !firstWeek &&
    queue.length === 0 &&
    !snoozed &&
    !rerunNotice &&
    !replies.notice &&
    !showNeedsYouMenu;
  // The first week's empty queue points at the own-mailbox step only while
  // that mailbox isn't connected (null until known, so no flash).
  const [ownMailboxConnected, setOwnMailboxConnected] = useState<boolean | null>(null);
  useEffect(() => {
    if (!firstWeek) return;
    const ctrl = new AbortController();
    getDelegation(ctrl.signal)
      .then((d) => setOwnMailboxConnected(d ? d.gmail.status === "connected" : null))
      .catch(() => undefined);
    return () => ctrl.abort();
  }, [firstWeek]);

  // /today's `quiet_banner`: a quiet day, a department set up, and not the
  // first week, whose card says what to start on instead.
  const quietBanner =
    today?.quiet_banner ? (
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-2xl border border-emerald-500/25 bg-emerald-500/5 px-4 py-3">
        <span className={`text-[15px] ${TONE_TEXT.emerald}`}>Quiet day: nothing waits for your decision.</span>
        {!solo && (
          <Link href="/departments" className="text-sm font-medium text-accent hover:underline">
            Set up a check-in →
          </Link>
        )}
      </div>
    ) : null;
  const bannerSlot = banner ?? quietBanner;

  const onSummaryPart = (part: SummaryPart) => {
    if (part.target.kind === "needs_you") {
      document.getElementById(NEEDS_YOU_ID)?.scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (part.target.kind === "panel" && part.target.panel) {
      setOpenPanel(part.target.panel);
    }
  };

  const panelFooterLink = (href: string, label: string) => (
    <Link href={href} className={buttonClass("secondary", "md", "w-full sm:w-auto")}>
      {label}
    </Link>
  );

  // Home's panels (the brief and the tiles'), each shown in a side panel on
  // Home and as a page of its own at /briefing/<panel>.
  const homePanels: Record<BriefingPanel, PanelView> | null = today
    ? {
        // Right now's rows (what waits with no card), also listed under
        // "Your decision" on Home; this page keeps /briefing/brief working.
        brief: {
          title: "Right now",
          subtitle: dateLabel,
          width: "lg",
          body:
            rightNowRows.length > 0 ? (
              <div className="space-y-3">
                {rightNowRows.map((row) => (
                  <RightNowItem key={row.key} row={row} actions={rightNowActions} />
                ))}
              </div>
            ) : (
              <p className="text-[15px] text-fg-muted">Nothing waits for your decision right now.</p>
            ),
        },
        departments: {
          title: "Departments",
          width: "lg",
          footer: panelFooterLink("/departments", "View all departments"),
          body: <DepartmentsPanelBody departments={departments} onContinue={onContinue} />,
        },
        people: {
          title: "People",
          width: "lg",
          footer: panelFooterLink("/people", "View all people"),
          body: <PeoplePanelBody people={today.people} />,
        },
        team: {
          title: "Across the team",
          subtitle: "Waiting on someone else",
          width: "lg",
          body: (
            <div className="space-y-4">
              {otherProposals.map((p) => (
                <ProposalCard
                  key={p.alert_id}
                  proposal={p}
                  people={today.people}
                  onContinue={onContinue}
                  // Someone else's card offers Approve only to the
                  // principal (its `actions`); the server refuses anyone else.
                  onApprove={handleApprove}
                  onDismiss={handleDismiss}
                  onSnooze={handleSnooze}
                  onRerun={handleRerun}
                  onApproveWithEdits={handleApproveWithEdits}
                  onAnswer={handleAnswer}
                />
              ))}
              {otherProposals.length === 0 && (
                <p className="text-[15px] text-fg-muted">
                  Nothing waiting on anyone else.
                  {today.people.length <= 1 && " Add your team on People to see their work here."}
                </p>
              )}
            </div>
          ),
        },
        inFlight: { title: "Under way", body: <InFlightPanelBody inFlight={inFlight} /> },
        handled: {
          title: "Handled",
          body: (
            <HandledPanelBody rows={handledRows} onReopen={handleReopen} undone={undoneRows} onJumpToAlert={jumpToAlert} />
          ),
        },
        monitoring: {
          title: "Monitoring",
          body: (
            <MonitoringPanelBody
              proposals={monitoringProposals}
              onContinue={onContinue}
              onDismiss={handleDismiss}
              onBulkDismiss={handleBulkDismiss}
            />
          ),
        },
        clients: {
          title: "Across your clients",
          footer: panelFooterLink("/clients", "Manage clients"),
          body: <PracticeClientsPanelBody clients={practiceClients} />,
        },
      }
    : null;

  if (panel) {
    const view = homePanels?.[panel];
    return (
      <div className="flex flex-col h-full bg-surface">
        <main className="flex-1 overflow-y-auto">
          <div className="max-w-3xl mx-auto px-4 sm:px-6 py-6 sm:py-10">
            {loading && <p className="text-fg-muted text-[15px]">Loading…</p>}
            {error && (
              <div className="mb-4 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-[15px] text-rose-700 dark:text-rose-300">
                {error}
              </div>
            )}
            {view && (
              <>
                <header className="mb-4">
                  <h1 className="text-2xl font-bold tracking-tight text-fg">{view.title}</h1>
                  {view.subtitle && <p className="mt-1 text-sm text-fg-muted">{view.subtitle}</p>}
                </header>
                {snoozed && (
                  <p className="mb-3 flex flex-wrap items-center gap-x-3 text-sm text-fg-muted" role="status">
                    Snoozed until {snoozed.short}.
                    <button type="button" onClick={handleUnsnooze} className="font-medium text-accent hover:underline">
                      Undo
                    </button>
                  </p>
                )}
                {rerunNotice && (
                  <p className="mb-3 text-sm text-fg-muted" role="status">
                    {rerunNotice}
                  </p>
                )}
                {view.body}
                {view.footer && <div className="mt-6">{view.footer}</div>}
              </>
            )}
            <p className="mt-8 text-sm">
              <Link href="/" className="text-accent hover:underline">
                Back to Home
              </Link>
            </p>
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 py-6 sm:py-10">
          <header className="mb-6">
            <p className="text-sm font-medium text-fg-muted">{dateLabel}</p>
            <h1 className="mt-1 text-2xl sm:text-3xl font-bold tracking-tight text-fg">
              {showHeader ? greeting(firstName) : "Today"}
            </h1>
            {today && (
              <p className="mt-2 text-base sm:text-lg text-fg-muted leading-snug">
                {summary.length === 0
                  ? firstWeekCard
                    ? "Your first week. Start below."
                    : "All clear: nothing waits for your decision right now."
                  : summary.map((part, i) => (
                      <span key={part.text}>
                        {part.target.kind === "href" ? (
                          <Link href={part.target.href ?? "/"} className="hover:text-fg hover:underline underline-offset-4">
                            {part.text}
                          </Link>
                        ) : (
                          <button
                            type="button"
                            onClick={() => onSummaryPart(part)}
                            className="cursor-pointer text-left hover:text-fg hover:underline underline-offset-4"
                          >
                            {part.text}
                          </button>
                        )}
                        {i < summary.length - 1 ? ". " : "."}
                      </span>
                    ))}
              </p>
            )}
          </header>

          {onContinue && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                submitAsk();
              }}
              className="mb-6 flex items-center gap-2 rounded-2xl border border-line bg-surface-elevated p-2 pl-4 shadow-sm focus-within:border-accent/60 focus-within:ring-2 focus-within:ring-accent/20"
            >
              <input
                type="text"
                value={askText}
                onChange={(e) => setAskText(e.target.value)}
                placeholder="Ask your Executive anything…"
                aria-label="Ask your Executive"
                className="min-w-0 flex-1 bg-transparent py-2 text-base text-fg placeholder:text-fg-muted focus:outline-none"
              />
              <button
                type="submit"
                disabled={!askText.trim()}
                aria-label="Ask"
                className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-xl bg-accent-strong text-white transition-colors hover:bg-accent-strong/90 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <Icon name="arrow-send" size="w-4 h-4" />
              </button>
            </form>
          )}

          {loading && <p className="text-fg-muted text-[15px]">Loading…</p>}
          {error && (
            <div className="mb-4 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-[15px] text-rose-700 dark:text-rose-300">
              {error}
            </div>
          )}

          {firstWeekCard && <FirstWeekCard card={firstWeekCard} />}

          {bannerSlot && <div className="mb-6">{bannerSlot}</div>}

          {today && (
            <>
              {/* Needs you — decisions routed to the caller, and replies
                  waiting to be sent. */}
              {!hideEmptyNeedsYou && (
                <section id={NEEDS_YOU_ID} className="mb-8 scroll-mt-4">
                <div className="mb-3 flex items-center gap-2">
                  <h2 className="text-lg font-semibold text-fg">
                    Your decision
                    {needsYouCount > 0 && <span className="ml-1.5 font-normal text-fg-muted">({needsYouCount})</span>}
                  </h2>
                  {replies.cards.length > 0 && <InfoTip align="left">{REPLIES_WAITING_TIP}</InfoTip>}
                  {inviteCards.cards.length > 0 && <InfoTip align="left">{INVITE_CARDS_TIP}</InfoTip>}
                  {actionCards.cards.length > 0 && <InfoTip align="left">{ACTION_CARDS_TIP}</InfoTip>}
                  {draftCards.cards.length > 0 && <InfoTip align="left">{draftsTip}</InfoTip>}
                  <div className="ml-auto flex items-center gap-1">
                    {showNeedsYouMenu && <OverflowMenu size="sm" label="Your decision options" items={needsYouMenu} />}
                  </div>
                </div>
                {snoozed && (
                  <p className="mb-3 flex flex-wrap items-center gap-x-3 text-sm text-fg-muted" role="status">
                    Snoozed until {snoozed.short}.
                    <button
                      type="button"
                      onClick={handleUnsnooze}
                      className="font-medium text-accent hover:underline"
                    >
                      Undo
                    </button>
                  </p>
                )}
                {rerunNotice && (
                  <p className="mb-3 text-sm text-fg-muted" role="status">
                    {rerunNotice}
                  </p>
                )}
                {replies.notice && (
                  <p className={`mb-3 text-sm ${TONE_TEXT.emerald}`} role="status">
                    {replies.notice}
                  </p>
                )}
                {queue.length > 0 ? (
                  <div className="space-y-4">
                    {visibleQueue.map((item, i) => (
                      <div key={item.key}>{item.render(i === 0)}</div>
                    ))}
                    {(hiddenCount > 0 || (showAllNeedsYou && currentQueue.length > NEEDS_YOU_VISIBLE)) && (
                      <div className="flex justify-center">
                        <button
                          type="button"
                          onClick={() => setShowAllNeedsYou((v) => !v)}
                          className={buttonClass("secondary", "md")}
                        >
                          {showAllNeedsYou ? "Show fewer" : `Show ${hiddenCount} more`}
                        </button>
                      </div>
                    )}
                    {olderQueue.length > 0 && (
                      <div className="rounded-2xl border border-line bg-surface-elevated">
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
                          <div className="min-w-0 flex-1">
                            <p className="text-[15px] font-semibold text-fg">Older ({olderQueue.length})</p>
                            <p className="text-sm text-fg-muted">Untouched for {NEEDS_YOU_OLDER_AFTER_DAYS} days or more</p>
                          </div>
                          <button
                            type="button"
                            onClick={() => setShowOlder((v) => !v)}
                            aria-expanded={showOlder}
                            className={buttonClass("secondary", "sm")}
                          >
                            {showOlder ? "Hide" : "Show"}
                          </button>
                          <button
                            type="button"
                            onClick={() => void handleClearOlder()}
                            disabled={clearingOlder}
                            className={buttonClass("ghost", "sm")}
                          >
                            {clearingOlder ? "Clearing…" : "Clear all"}
                          </button>
                        </div>
                        {olderNotice && (
                          <p className="px-4 pb-3 text-sm text-red-500" role="status">
                            {olderNotice}
                          </p>
                        )}
                        {showOlder && (
                          <div className="space-y-4 border-t border-line p-3">
                            {olderQueue.map((item) => (
                              <div key={item.key}>{item.render(false)}</div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ) : firstWeek ? (
                  <p className="rounded-2xl border border-dashed border-line px-4 py-5 text-[15px] text-fg-muted">
                    Nothing waits for your decision yet. Decisions and replies to send land here.
                    {ownMailboxConnected === false && (
                      <>
                        {" "}
                        <Link href={FIRST_RUN_LINKS.ownMailbox} className="font-medium text-accent hover:underline">
                          Let it read your mailbox
                        </Link>{" "}
                        to get draft replies here.
                      </>
                    )}
                  </p>
                ) : !isQuiet ? (
                  <p className="rounded-2xl border border-dashed border-line px-4 py-5 text-[15px] text-fg-muted">
                    Nothing waits for your decision right now. Decisions and replies to send land here.
                  </p>
                ) : null}
                </section>
              )}

              <DoneForYou
                rows={doneRows}
                onRetry={(row) => void handleRetryApproval(row)}
                onOpenChat={onOpenChat}
              />

              {/* Everything else, one quiet line: each opens its panel or page. */}
              {tiles.length > 0 && (
                <nav aria-label="More" className="mb-8 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-fg-muted">
                  {tiles.map((t) => (
                    <button
                      key={t.key}
                      type="button"
                      onClick={() => setOpenPanel(t.key)}
                      aria-haspopup="dialog"
                      className="hover:text-fg hover:underline underline-offset-4"
                    >
                      {`${t.value} ${t.label}`}
                    </button>
                  ))}
                </nav>
              )}

              {HOME_PANEL_KEYS.map((key) => {
                const view = homePanels?.[key];
                return view ? (
                  <SidePanel
                    key={key}
                    open={openPanel === key}
                    onClose={() => setOpenPanel(null)}
                    title={view.title}
                    subtitle={view.subtitle}
                    width={view.width}
                    footer={view.footer}
                  >
                    {view.body}
                  </SidePanel>
                ) : null;
              })}
              {solo && (
                <>
                  <SidePanel
                    open={openPanel === "projects"}
                    onClose={() => setOpenPanel(null)}
                    title="Your projects"
                    footer={panelFooterLink("/goals", "Goals")}
                  >
                    <ProjectsPanelBody state={projects} />
                  </SidePanel>
                  <SidePanel
                    open={openPanel === "dueSoon"}
                    onClose={() => setOpenPanel(null)}
                    title="Due soon"
                    footer={principalId != null ? panelFooterLink(`/people/${principalId}`, "All open items") : undefined}
                  >
                    <DueSoonPanelBody state={openLoops} />
                  </SidePanel>
                  {weeklyReview && (
                    <SidePanel
                      open={openPanel === "weeklyReview"}
                      onClose={() => setOpenPanel(null)}
                      title="This week's review"
                    >
                      <WeeklyReviewPanelBody review={weeklyReview} />
                    </SidePanel>
                  )}
                </>
              )}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
