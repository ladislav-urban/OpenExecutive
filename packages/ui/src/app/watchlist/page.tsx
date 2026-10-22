"use client";

import Link from "next/link";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import Switch from "@/components/Switch";
import Button from "@/components/ui/Button";
import OverflowMenu, { type OverflowItem } from "@/components/ui/OverflowMenu";

import {
  approveWatchSuggestion,
  createWatchlistItem,
  declineWatchSuggestion,
  deleteWatchlistItem,
  describeWatch,
  draftToCreate,
  getWatchForm,
  listDepartments,
  listWatchlist,
  patchWatchlistItem,
  previewWatch,
  type WatchDeclineReason,
  type WatchDraft,
  type WatchForm as WatchFormChoices,
  type WatchPreview,
  type WatchlistCadence,
  type WatchlistItem,
  type WatchlistPatch,
  type WatchlistSeverity,
  type WatchlistSignalType,
} from "@/lib/api";

// How a watch shows, and what the form offers, come from the API
// (monitoring/presentation.py): each watch's words, group and menus
// (GET /watchlist), the form's choices (GET /watchlist/form), and what the
// form's values come to (POST /watchlist/preview). This page holds none of
// those rules.

const fieldCls =
  "w-full px-3.5 py-2.5 text-[15px] rounded-xl bg-surface border border-line text-fg placeholder:text-fg-subtle focus:outline-none focus:ring-2 focus:ring-accent/40";
const labelCls = "block text-sm font-medium text-fg-muted mb-1.5";

// Rationale + provenance stamp the research policy left on the row.
function policyStamp(item: WatchlistItem): { entity?: string; score?: number; source_url?: string } {
  const raw = (item.config_json as Record<string, unknown> | undefined)?._policy;
  return raw && typeof raw === "object" ? (raw as { entity?: string; score?: number; source_url?: string }) : {};
}

// Turn a kebab-case slug into a readable title: "stock-aapl" → "stock aapl".
// The full slug stays the identity (used for routing + shown on hover).
function humanizeSlug(slug: string): string {
  return slug.replace(/-+/g, " ").trim();
}

// Relative time. Kept inline so the watchlist page is self-contained;
// Briefing.tsx has its own copy with the same formula.
function formatRelTime(iso: string | null): string {
  if (!iso) return "never";
  try {
    const diff = new Date(iso).getTime() - Date.now();
    const abs = Math.abs(diff);
    if (abs < 60_000) return "now";
    if (abs < 3_600_000) return `${Math.round(abs / 60_000)}m`;
    if (abs < 86_400_000) return `${Math.round(abs / 3_600_000)}h`;
    return `${Math.round(abs / 86_400_000)}d`;
  } catch {
    return "—";
  }
}

/** A watch's decline reasons as ⋯ items, each saying what it does. */
function declineItems(
  item: WatchlistItem,
  busy: boolean,
  onPick: (slug: string, reason: WatchDeclineReason, patch: WatchlistPatch | null) => void,
): OverflowItem[] {
  return (item.decline?.reasons ?? []).map((r) => ({
    label: r.menu_label,
    disabled: busy,
    onSelect: () => onPick(item.slug, r.value, r.patch ?? null),
  }));
}

function SuggestionCard({
  item,
  busy,
  onApprove,
  onDecline,
  departmentTitle,
}: {
  item: WatchlistItem;
  busy: boolean;
  onApprove: (slug: string) => void;
  onDecline: (slug: string, reason: WatchDeclineReason, patch: WatchlistPatch | null) => void;
  departmentTitle?: string;
}) {
  const stamp = policyStamp(item);
  return (
    <div className="flex min-w-0 flex-col rounded-2xl border border-amber-500/40 bg-surface-elevated p-5 shadow-sm">
      <div className="flex items-start justify-between gap-2 mb-1">
        <div className="min-w-0">
          <div className="text-lg font-semibold text-fg truncate" title={item.slug}>
            {humanizeSlug(item.slug)}
          </div>
          <div className="text-sm text-fg-muted truncate" title={item.target}>
            {item.group?.label ?? item.signal_type} · {item.target}
          </div>
        </div>
        <span className="flex-shrink-0 rounded-full bg-amber-500/15 px-2.5 py-0.5 text-xs font-medium text-amber-600 dark:text-amber-300">
          Suggested
        </span>
      </div>
      {item.notes && <p className="text-[15px] text-fg mt-2">{item.notes}</p>}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-fg-muted mt-2">
        {stamp.entity && <span>about: {stamp.entity}</span>}
        {item.route_to_department && (
          <span title="This department's head was asked to review it">
            for: {departmentTitle ?? item.route_to_department}
          </span>
        )}
        <span>suggested {formatRelTime(item.created_at)} ago</span>
        <span>seen in shadow: {item.fired_count} signal{item.fired_count === 1 ? "" : "s"}</span>
        {stamp.source_url && (
          <a href={stamp.source_url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            source ↗
          </a>
        )}
      </div>
      <div className="mt-auto flex items-center gap-2 pt-4">
        <Button variant="primary" disabled={busy} onClick={() => onApprove(item.slug)}>
          {busy ? "…" : "Approve"}
        </Button>
        <OverflowMenu
          label={`Decline ${humanizeSlug(item.slug)}`}
          items={declineItems(item, busy, onDecline)}
        />
      </div>
    </div>
  );
}

function WatchCard({
  item,
  onToggle,
  toggleBusy,
  onStopWatching,
}: {
  item: WatchlistItem;
  onToggle: (slug: string, enabled: boolean) => void;
  toggleBusy: boolean;
  onStopWatching?: (slug: string, reason: WatchDeclineReason, patch: WatchlistPatch | null) => void;
}) {
  // Mode, severity, cadence and counts live on the monitor's own page.
  const isResearch = Boolean(item.added_by_executive);
  const labelId = useId();
  const href = `/watchlist/${encodeURIComponent(item.slug)}`;
  return (
    <div className="flex min-w-0 items-start gap-3 rounded-2xl border border-line bg-surface-elevated p-5 shadow-sm">
      <Link href={href} className="min-w-0 flex-1 group">
        <div
          id={labelId}
          className="text-lg font-semibold text-fg group-hover:text-accent transition-colors truncate"
          title={item.slug}
        >
          {humanizeSlug(item.slug)}
        </div>
        <div className="text-sm text-fg-muted mt-0.5 truncate" title={item.target}>
          {item.target}
        </div>
        <div className="text-sm text-fg-subtle mt-1.5">
          {item.last_fired_at
            ? `Last fired ${formatRelTime(item.last_fired_at)} ago`
            : "Hasn't fired yet"}
          {isResearch && " · added by the Executive"}
        </div>
      </Link>
      <div className="relative flex flex-shrink-0 items-center gap-1 pt-1">
        <span className="sr-only">{item.enabled ? "On" : "Off"}</span>
        {/* The label pads the small switch out to a 40px tap target. */}
        <label className="inline-flex h-10 w-12 cursor-pointer items-center justify-center">
          <Switch
            checked={item.enabled}
            disabled={toggleBusy}
            labelledBy={labelId}
            onChange={(on) => onToggle(item.slug, on)}
          />
        </label>
        <OverflowMenu
          label={`More for ${humanizeSlug(item.slug)}`}
          items={[
            { label: "Open details", href },
            ...(isResearch && onStopWatching ? declineItems(item, toggleBusy, onStopWatching) : []),
          ]}
        />
      </div>
    </div>
  );
}

/** The form's starting values; a draft from "Change something" fills them. */
interface FormInitial {
  slug?: string;
  signalType?: WatchlistSignalType;
  target?: string;
  displayLabel?: string;
  trigger?: Record<string, unknown>;
  cadence?: WatchlistCadence;
  severityFloor?: WatchlistSeverity;
  severityCeiling?: WatchlistSeverity;
  mode?: "active" | "dry_run";
  notes?: string;
}

/** The trigger box's text as an object, or null while it isn't valid JSON. */
function parseTrigger(text: string): Record<string, unknown> | null {
  if (!text.trim()) return {};
  try {
    const parsed: unknown = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

interface WatchFormProps {
  choices: WatchFormChoices;
  initial?: FormInitial;
  onCreated: (w: WatchlistItem) => void;
}

/** What the form's values come to, asked again a moment after they change. */
function useWatchPreview(values: Parameters<typeof previewWatch>[0] | null): WatchPreview | null {
  const [preview, setPreview] = useState<WatchPreview | null>(null);
  const key = JSON.stringify(values);
  useEffect(() => {
    if (values === null) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      previewWatch(values, controller.signal).then(setPreview).catch(() => { /* keep the last */ });
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // Keyed on the values themselves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return preview;
}

function WatchForm({ choices, initial, onCreated }: WatchFormProps) {
  const defaults = choices.defaults;
  const [slug, setSlug] = useState(initial?.slug ?? "");
  const [signalType, setSignalType] = useState<WatchlistSignalType>(initial?.signalType ?? defaults.signal_type);
  const [target, setTarget] = useState(initial?.target ?? "");
  const [displayLabel, setDisplayLabel] = useState(initial?.displayLabel ?? "");
  const [trigger, setTrigger] = useState(
    initial?.trigger && Object.keys(initial.trigger).length ? JSON.stringify(initial.trigger) : "",
  );
  const [cadence, setCadence] = useState<WatchlistCadence>(initial?.cadence ?? defaults.cadence);
  const [severityFloor, setSeverityFloor] = useState<WatchlistSeverity>(initial?.severityFloor ?? defaults.severity_floor);
  const [severityCeiling, setSeverityCeiling] = useState<WatchlistSeverity>(
    initial?.severityCeiling ?? defaults.severity_ceiling,
  );
  const [mode, setMode] = useState<"active" | "dry_run">(initial?.mode ?? defaults.mode);
  const [notes, setNotes] = useState(initial?.notes ?? "");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Slug, trigger, cadence, mode and severity sit under "More options" with
  // their defaults; it opens by itself when an error points into it, or
  // when a draft brought a trigger with it.
  const [moreOpen, setMoreOpen] = useState(Boolean(initial?.trigger && Object.keys(initial.trigger).length));

  const option = choices.signal_types.find((s) => s.value === signalType);
  const targetHint = option?.hint ?? "";
  const parsedTrigger = parseTrigger(trigger);
  const preview = useWatchPreview(
    parsedTrigger === null
      ? null
      : {
          signal_type: signalType,
          target,
          trigger: parsedTrigger,
          cadence,
          severity_floor: severityFloor,
          severity_ceiling: severityCeiling,
        },
  );
  // The slug an empty slug field gets (the API makes the same one).
  const slugSuggestion = target.trim() ? preview?.slug ?? "" : "";

  async function submit() {
    setSaving(true);
    setErr(null);
    if (parsedTrigger === null) {
      setErr("Trigger must be valid JSON, e.g. {\"abs_change_pct_gte\": 5}");
      setMoreOpen(true);
      setSaving(false);
      return;
    }
    try {
      const created = await createWatchlistItem({
        slug: slug.trim(),
        signal_type: signalType,
        target: target.trim(),
        display_label: displayLabel.trim(),
        trigger: parsedTrigger,
        cadence,
        severity_floor: severityFloor,
        severity_ceiling: severityCeiling,
        mode,
        notes: notes.trim(),
      });
      onCreated(created);
    } catch (e) {
      const message = e instanceof Error ? e.message : "Create failed";
      setErr(message);
      if (/slug|trigger|cadence|severity|mode/i.test(message)) setMoreOpen(true);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-4 pt-3">
      <div>
        <label htmlFor="watch-signal-type" className={labelCls}>Signal type</label>
        <select
          id="watch-signal-type"
          value={signalType}
          onChange={(e) => setSignalType(e.target.value as WatchlistSignalType)}
          className={fieldCls}
        >
          {choices.signal_types.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
      </div>
      <div>
        <label htmlFor="watch-target" className={labelCls}>Target</label>
        <input
          id="watch-target"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          placeholder={targetHint}
          className={fieldCls}
        />
        <p className="text-sm text-fg-subtle mt-1.5">{targetHint}</p>
        {option?.billed && (
          <p className="text-sm text-amber-600 dark:text-amber-300 mt-1.5">
            ⚠ Billed: runs an LLM web search on every poll. Prefer a slower
            cadence (daily / weekly) and a tight query.
          </p>
        )}
      </div>
      <div>
        <label htmlFor="watch-label" className={labelCls}>Name (optional)</label>
        <input
          id="watch-label"
          value={displayLabel}
          onChange={(e) => setDisplayLabel(e.target.value)}
          maxLength={80}
          placeholder="e.g. Initech"
          className={fieldCls}
        />
      </div>
      <div>
        <label htmlFor="watch-notes" className={labelCls}>Notes</label>
        <input
          id="watch-notes"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          maxLength={500}
          placeholder="Why watch this?"
          className={fieldCls}
        />
      </div>

      <details
        className="rounded-xl border border-line px-4 py-2"
        open={moreOpen}
        onToggle={(e) => setMoreOpen((e.target as HTMLDetailsElement).open)}
      >
        <summary className="flex min-h-10 cursor-pointer flex-wrap items-center gap-x-2 text-[15px] font-medium text-fg">
          More options
          <span className="min-w-0 truncate text-sm font-normal text-fg-subtle">
            {slug.trim() || slugSuggestion || "name"} · {cadence} · {mode} · {severityFloor}→{severityCeiling}
          </span>
        </summary>
        <div className="space-y-4 py-3">
          <div>
            <label htmlFor="watch-slug" className={labelCls}>Slug (kebab-case)</label>
            <input
              id="watch-slug"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder={slugSuggestion || "stock-aapl"}
              className={fieldCls}
            />
            <p className="text-sm text-fg-subtle mt-1.5">
              Left empty, it is named {slugSuggestion ? <code>{slugSuggestion}</code> : "from the type and target"}.
            </p>
          </div>
          <div>
            <label htmlFor="watch-trigger" className={labelCls}>Trigger (JSON, optional)</label>
            <textarea
              id="watch-trigger"
              value={trigger}
              onChange={(e) => setTrigger(e.target.value)}
              placeholder={option?.trigger_example ?? "{}"}
              rows={3}
              className={`${fieldCls} font-mono text-sm`}
            />
            <p className="text-sm text-fg-subtle mt-1.5">
              {parsedTrigger === null
                ? "Not valid JSON yet."
                : preview
                  ? `Tells you when: ${preview.trigger_words}.`
                  : " "}
            </p>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label htmlFor="watch-cadence" className={labelCls}>Cadence</label>
              <select
                id="watch-cadence"
                value={cadence}
                onChange={(e) => setCadence(e.target.value as WatchlistCadence)}
                className={fieldCls}
              >
                {choices.cadences.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.value}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="watch-mode" className={labelCls}>Mode</label>
              <select
                id="watch-mode"
                value={mode}
                onChange={(e) => setMode(e.target.value as "active" | "dry_run")}
                className={fieldCls}
              >
                <option value="active">active</option>
                <option value="dry_run">dry_run</option>
              </select>
            </div>
            <div>
              <label htmlFor="watch-floor" className={labelCls}>Severity floor</label>
              <select
                id="watch-floor"
                value={severityFloor}
                onChange={(e) => setSeverityFloor(e.target.value as WatchlistSeverity)}
                className={fieldCls}
              >
                {choices.severities.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.value}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="watch-ceiling" className={labelCls}>Severity ceiling</label>
              <select
                id="watch-ceiling"
                value={severityCeiling}
                onChange={(e) => setSeverityCeiling(e.target.value as WatchlistSeverity)}
                className={fieldCls}
              >
                {choices.severities.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.value}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>
      </details>

      {err && (
        <div className="px-4 py-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px]">
          {err}
        </div>
      )}

      <div className="flex justify-end">
        <Button
          variant="primary"
          disabled={saving || !target.trim()}
          onClick={submit}
        >
          {saving ? "Adding…" : "Add monitor"}
        </Button>
      </div>
    </div>
  );
}

/** "Here's what I'll watch": a draft in plain words, not saved yet. */
function DraftCard({
  draft,
  billed,
  busy,
  onStart,
  onChange,
}: {
  draft: WatchDraft;
  // Its signal type is billed on every poll (GET /watchlist/form).
  billed: boolean;
  busy: boolean;
  onStart: () => void;
  onChange: () => void;
}) {
  const lines: [string, string][] = [
    ["Watching", draft.watching],
    ["Tells you when", draft.tell_you_when],
    ["How often", draft.how_often],
    ["How loud", draft.how_loud],
  ];
  return (
    <div className="mt-4 rounded-2xl border border-accent/40 bg-surface p-4 sm:p-5">
      <h3 className="text-base font-semibold text-fg mb-3">Here&apos;s what I&apos;ll watch</h3>
      <dl className="space-y-2.5">
        {lines.map(([label, value]) => (
          <div key={label} className="grid grid-cols-1 gap-0.5 sm:grid-cols-[9rem_1fr] sm:gap-3">
            <dt className="text-sm text-fg-muted">{label}</dt>
            <dd className="min-w-0 break-words text-[15px] text-fg">{value}</dd>
          </div>
        ))}
      </dl>
      {billed && (
        <p className="text-sm text-amber-600 dark:text-amber-300 mt-3">
          This one runs a web search each time it looks, which costs a little each time.
        </p>
      )}
      <div className="mt-4 flex flex-wrap gap-2">
        <Button variant="primary" disabled={busy} onClick={onStart}>
          {busy ? "Starting…" : "Start watching"}
        </Button>
        <Button variant="ghost" disabled={busy} onClick={onChange}>
          Change something
        </Button>
      </div>
    </div>
  );
}

/** "What should I watch?" — a sentence in, a draft out, then the by-hand form. */
function SetUpWatch({ choices, onCreated }: { choices: WatchFormChoices | null; onCreated: (w: WatchlistItem) => void }) {
  const [text, setText] = useState("");
  const [asking, setAsking] = useState(false);
  const [starting, setStarting] = useState(false);
  const [draft, setDraft] = useState<WatchDraft | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [byHandOpen, setByHandOpen] = useState(false);
  // Remounts the form with a draft's values when "Change something" opens it.
  const [formSeed, setFormSeed] = useState<{ key: number; initial?: FormInitial }>({ key: 0 });
  const byHandRef = useRef<HTMLDetailsElement>(null);

  async function ask() {
    if (!text.trim() || asking) return;
    setAsking(true);
    setErr(null);
    setDraft(null);
    try {
      setDraft(await describeWatch(text.trim()));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Couldn't read that just now.");
    } finally {
      setAsking(false);
    }
  }

  async function start() {
    if (!draft) return;
    setStarting(true);
    setErr(null);
    try {
      const created = await createWatchlistItem(draftToCreate(draft));
      setDraft(null);
      setText("");
      onCreated(created);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Couldn't start watching.");
    } finally {
      setStarting(false);
    }
  }

  function changeSomething() {
    if (!draft) return;
    const i = draft.form_initial;
    setFormSeed((s) => ({
      key: s.key + 1,
      initial: i
        ? {
            slug: i.slug,
            signalType: i.signal_type,
            target: i.target,
            displayLabel: i.display_label,
            trigger: i.trigger,
            cadence: i.cadence,
            severityFloor: i.severity_floor,
            severityCeiling: i.severity_ceiling,
            mode: i.mode,
            notes: i.notes,
          }
        : undefined,
    }));
    setDraft(null);
    setByHandOpen(true);
    requestAnimationFrame(() => byHandRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }

  return (
    <section className="mb-8 rounded-2xl border border-line bg-surface-elevated p-4 sm:p-5 shadow-sm">
      <label htmlFor="watch-describe" className="block text-base font-semibold text-fg mb-2">
        What should I watch?
      </label>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          id="watch-describe"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void ask();
          }}
          maxLength={500}
          placeholder="e.g. Tell me if Initech's stock moves more than 8%"
          className={`${fieldCls} min-w-0 flex-1`}
        />
        <Button
          variant="primary"
          className="flex-shrink-0"
          disabled={asking || !text.trim()}
          onClick={() => void ask()}
        >
          {asking ? "Reading…" : "Set it up"}
        </Button>
      </div>
      {err && (
        <div className="mt-3 px-4 py-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px]">
          {err}
        </div>
      )}
      {draft && (
        <DraftCard
          draft={draft}
          billed={Boolean(choices?.signal_types.find((s) => s.value === draft.signal_type)?.billed)}
          busy={starting}
          onStart={() => void start()}
          onChange={changeSomething}
        />
      )}
      <details
        ref={byHandRef}
        className="mt-4 rounded-xl border border-line px-4 py-2"
        open={byHandOpen}
        onToggle={(e) => setByHandOpen((e.target as HTMLDetailsElement).open)}
      >
        <summary className="flex min-h-10 cursor-pointer items-center text-[15px] font-medium text-fg">
          Set it up by hand
        </summary>
        {choices ? (
        <WatchForm
          key={formSeed.key}
          choices={choices}
          initial={formSeed.initial}
          onCreated={(w) => {
            setByHandOpen(false);
            setFormSeed((s) => ({ key: s.key + 1 }));
            onCreated(w);
          }}
        />
        ) : (
          <p className="py-3 text-[15px] text-fg-muted">Loading…</p>
        )}
      </details>
    </section>
  );
}

export default function WatchlistPage() {
  const [items, setItems] = useState<WatchlistItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busySlugs, setBusySlugs] = useState<Set<string>>(new Set());
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  // slug → title for the "for: <department>" label on routed watches.
  const [departmentTitles, setDepartmentTitles] = useState<Record<string, string>>({});

  const toggleCollapsed = (key: string) =>
    setCollapsed((c) => ({ ...c, [key]: !c[key] }));

  // Monitors under the group each one names (`group`, by signal type), in
  // the groups' order; suggestions wait above them on their own.
  const suggestions = useMemo(() => items.filter((it) => it.is_suggestion), [items]);

  const groups = useMemo(() => {
    const map = new Map<string, { key: string; label: string; order: number; items: WatchlistItem[] }>();
    for (const it of items) {
      if (it.is_suggestion) continue;
      const g = it.group ?? { key: it.signal_type, label: it.signal_type, order: Number.MAX_SAFE_INTEGER };
      const bucket = map.get(g.key) ?? { ...g, items: [] };
      bucket.items.push(it);
      map.set(g.key, bucket);
    }
    return Array.from(map.values()).sort((a, b) => a.order - b.order || a.key.localeCompare(b.key));
  }, [items]);
  // The add form's choices (GET /watchlist/form).
  const [choices, setChoices] = useState<WatchFormChoices | null>(null);

  function refresh() {
    setLoading(true);
    listWatchlist()
      .then(setItems)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    refresh();
    getWatchForm().then(setChoices).catch(() => { /* the form waits */ });
    listDepartments()
      .then((states) =>
        setDepartmentTitles(Object.fromEntries(states.map((d) => [d.config.slug, d.config.title]))),
      )
      .catch(() => {});
  }, []);

  function markBusy(slug: string, busy: boolean) {
    setBusySlugs((prev) => {
      const next = new Set(prev);
      if (busy) next.add(slug);
      else next.delete(slug);
      return next;
    });
  }

  async function approve(slug: string) {
    markBusy(slug, true);
    try {
      const updated = await approveWatchSuggestion(slug);
      setItems((prev) => prev.map((it) => (it.slug === slug ? updated : it)));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Approve failed");
    } finally {
      markBusy(slug, false);
    }
  }

  async function decline(slug: string, reason: WatchDeclineReason) {
    markBusy(slug, true);
    try {
      const res = await declineWatchSuggestion(slug, reason);
      if (res.result === "removed") {
        setItems((prev) => prev.filter((it) => it.slug !== slug));
      } else {
        // too_noisy: the row went live with a high floor — reload it.
        refresh();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Decline failed");
    } finally {
      markBusy(slug, false);
    }
  }

  async function stopWatching(slug: string, reason: WatchDeclineReason, patch: WatchlistPatch | null) {
    markBusy(slug, true);
    try {
      if (patch) {
        // The reason keeps the watch, changed (too noisy: quieter).
        const updated = await patchWatchlistItem(slug, patch);
        setItems((prev) => prev.map((it) => (it.slug === slug ? updated : it)));
      } else {
        await deleteWatchlistItem(slug, reason);
        setItems((prev) => prev.filter((it) => it.slug !== slug));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Remove failed");
    } finally {
      markBusy(slug, false);
    }
  }

  async function toggle(slug: string, enabled: boolean) {
    // Optimistic. The disabled prop on the input prevents a second
    // click landing while the PATCH is in flight, so the revert path
    // can safely flip back to the prior value.
    setBusySlugs((prev) => new Set(prev).add(slug));
    setItems((prev) =>
      prev.map((it) => (it.slug === slug ? { ...it, enabled } : it)),
    );
    try {
      const updated = await patchWatchlistItem(slug, { enabled });
      setItems((prev) => prev.map((it) => (it.slug === slug ? updated : it)));
    } catch (e) {
      setItems((prev) =>
        prev.map((it) => (it.slug === slug ? { ...it, enabled: !enabled } : it)),
      );
      setError(e instanceof Error ? e.message : "Toggle failed");
    } finally {
      setBusySlugs((prev) => {
        const next = new Set(prev);
        next.delete(slug);
        return next;
      });
    }
  }

  return (
    <div className="flex flex-col h-full bg-surface">
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-5xl mx-auto px-4 sm:px-6 py-6 sm:py-8">
          <div className="mb-6">
            <div className="max-w-2xl">
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-fg">
                What the Executive watches
              </h1>
              <p className="text-[15px] text-fg-muted mt-2">
                External conditions the Executive is monitoring. Signals that survive
                severity + triage become proposals in your briefing.
              </p>
            </div>
          </div>

          <SetUpWatch choices={choices} onCreated={(w) => setItems((prev) => [...prev, w])} />

          {loading && <p className="text-fg-muted text-[15px]">Loading…</p>}
          {error && (
            <div className="px-4 py-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-500 text-[15px] mb-4">
              {error}
            </div>
          )}
          {!loading && !error && items.length === 0 && (
            <div className="rounded-2xl border border-dashed border-line p-8 text-center">
              <p className="text-fg-muted text-[15px] mb-3">Nothing being watched yet.</p>
              <p className="text-sm text-fg-subtle">
                Say what to watch in the box above, or ask the Executive in chat:{" "}
                <span className="italic">
                  Watch Initech&apos;s stock, alert me on 5% moves.
                </span>
              </p>
            </div>
          )}

          {suggestions.length > 0 && (
            <div className="mb-10">
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 mb-1">
                <h2 className="text-xl font-bold tracking-tight text-fg">Suggested by the Executive</h2>
                <span className="text-[15px] text-fg-muted">
                  {suggestions.length} waiting for you
                </span>
              </div>
              <p className="text-sm text-fg-subtle mb-4 max-w-3xl">
                Sources the research council thought worth monitoring but couldn&apos;t tie
                firmly enough to your company data to add on its own. They poll in
                shadow mode and never alert until you approve. Declines are remembered.
              </p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {suggestions.map((item) => (
                  <SuggestionCard
                    key={item.id}
                    item={item}
                    busy={busySlugs.has(item.slug)}
                    onApprove={approve}
                    onDecline={decline}
                    departmentTitle={departmentTitles[item.route_to_department]}
                  />
                ))}
              </div>
            </div>
          )}

          <div className="space-y-6">
            {groups.map((group) => {
              const isCollapsed = !!collapsed[group.key];
              return (
                <div key={group.key}>
                  <button
                    type="button"
                    aria-expanded={!isCollapsed}
                    onClick={() => toggleCollapsed(group.key)}
                    className="w-full min-h-10 flex items-center gap-2 mb-3 text-left"
                  >
                    <span
                      aria-hidden="true"
                      className={`text-fg-muted text-xs transition-transform ${
                        isCollapsed ? "" : "rotate-90"
                      }`}
                    >
                      ▶
                    </span>
                    <span className="text-base font-semibold text-fg">
                      {group.label}
                    </span>
                    <span className="text-sm text-fg-muted">
                      {group.items.length}{" "}
                      {group.items.length === 1 ? "monitor" : "monitors"}
                    </span>
                  </button>
                  {!isCollapsed && (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                      {group.items.map((item) => (
                        <WatchCard
                          key={item.id}
                          item={item}
                          onToggle={toggle}
                          toggleBusy={busySlugs.has(item.slug)}
                          onStopWatching={stopWatching}
                        />
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </main>
    </div>
  );
}
