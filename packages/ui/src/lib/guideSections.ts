// The User Guide's sections, in order (app/guide/page.tsx). Hardcoded so the
// sidebar renders at once without waiting for the API; the ids must match
// GUIDE_SECTIONS in packages/core/openexecutive/guide/sections.py, which
// scripts/guideSections.test.mjs checks. `make embed-outputs` lists the ids
// in embed-manifest.json (`guideSections`), so a host app linking to
// /guide?section=<id> can check its links still land.
//
// No imports, so `npm test` and scripts/embed-outputs.mjs can load it.

export const GUIDE_SECTIONS = [
  { id: "chat", label: "Chat & Briefing", sub: "The main surface — ask in plain language, and land on a briefing of what's happened." },
  { id: "ask_oe", label: "Help with this page", sub: "The sparkle button at the top right: explains any screen, answers how the app works, and fills forms for you to review." },
  { id: "today", label: "Today / Morning Brief", sub: "What needs you right now: proposals, department health, and people with open items." },
  { id: "pulse", label: "Memory", sub: "What it knows about you and the company, what you taught it, what it worked out, and what it does on its own." },
  { id: "review", label: "Review Queue", sub: "On Knowledge, under Advanced. Built-in knowledge is trusted by default. Review what you upload or edit, or send a domain for review yourself." },
  { id: "jobs", label: "Workflows", sub: "Multi-step workflows that produce a deliverable — board prep, GTM plan, perf review." },
  { id: "artifacts", label: "Documents", sub: "Your library of finished documents — drafts and workflow outputs in one place." },
  { id: "watchlist", label: "Watch List", sub: "External monitors — stock tickers, RSS feeds, status pages, web queries — that raise alerts." },
  { id: "departments", label: "Departments", sub: "Org units, each with goals, an authority level, and a specialist behind it." },
  { id: "people", label: "People", sub: "Your roster — the people it coordinates with, their SLAs, channels, and approval scopes." },
  { id: "company_profile", label: "Company Profile & Onboarding", sub: "Your company's identity and strategy — set up once, edited any time." },
  { id: "knowledge", label: "Knowledge base", sub: "Upload company documents so answers are grounded in your context." },
  { id: "skills", label: "How it's done", sub: "The method behind each workflow, also followed in chat. Edit it on the workflow's page." },
  { id: "custom_tools", label: "Custom Tools", sub: "When no tool fits, one is built — and kept when it will be needed again." },
  { id: "council", label: "Agent Council", sub: "Configure the specialists — models, prompts, reasoning depth, and the voice of its replies." },
  { id: "audit", label: "Audit Log", sub: "A searchable record of every turn, consult, tool call, alert, and scheduled action." },
  { id: "token_usage", label: "Token Usage", sub: "Where your spend goes — tokens and cost by day, model, and session." },
  { id: "simulator", label: "Company Simulator", sub: "Load a realistic test company to try everything before trusting it with real data." },
  { id: "clients", label: "Client Companies", sub: "Multi-client mode for fractional work — switch the live company between named client slots." },
  { id: "integrations", label: "Integrations", sub: "Use it where you already work — Slack, Discord, Telegram, email, Google Chat, MCP." },
  { id: "delegate", label: "Delegate", sub: "What it may do without asking you, writing email as you, and handling things on its own." },
  { id: "settings", label: "Settings & Advanced", sub: "Pausing, workspace, notifications, and the Advanced tools outside the day-to-day nav — including this guide." },
] as const;

export type GuideSectionId = (typeof GUIDE_SECTIONS)[number]["id"];

export const GUIDE_SECTION_IDS: readonly string[] = GUIDE_SECTIONS.map((s) => s.id);

/** The section `?section=<id>` asks to open at, or null (none, or not one of
 *  the guide's: the guide then opens at the top). A query parameter, not a
 *  hash: a host's frame passes the query through, and a hash can be lost on
 *  the way. */
export function sectionFromSearch(search: string): GuideSectionId | null {
  const id = new URLSearchParams(search).get("section");
  return id && GUIDE_SECTION_IDS.includes(id) ? (id as GuideSectionId) : null;
}

/** Whether to scroll to `target` now: it and every section above it have
 *  loaded (or failed), so nothing above it will still change its place. */
export function readyToScroll(target: string, settled: ReadonlySet<string>): boolean {
  const at = GUIDE_SECTION_IDS.indexOf(target);
  if (at < 0) return false;
  return GUIDE_SECTION_IDS.slice(0, at + 1).every((id) => settled.has(id));
}
