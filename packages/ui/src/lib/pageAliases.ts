// Old addresses that now open another page. next.config.ts serves them as
// redirects, which Next applies before the middleware and which carry the
// request's query over to the destination (merged with any query it has).
// That keeps `?embed=1` on the page a host's frame lands on: a page-level
// `redirect()` dropped it, and the frame then never answered the host
// (scripts/pageAliases.test.mjs).
//
// Links still hand some of these out (orchestrator/app_control_catalog.py,
// action chips), so they stay.
export const PAGE_ALIASES: ReadonlyArray<{ source: string; destination: string }> = [
  // Delegate opens on its first tab: what it may do without asking.
  { source: "/delegate", destination: "/delegate/what-it-may-do" },
  // The review queue lives inside the Knowledge base now.
  { source: "/review", destination: "/knowledge?view=review" },
  // Act as me moved to its own tab under Delegate.
  { source: "/settings/act-as-me", destination: "/delegate/act-as-me" },
  // On its own was folded into Your Executive.
  { source: "/settings/on-its-own", destination: "/settings/executive" },
  // Playbooks live on the workflow that follows them ("How it's done").
  { source: "/skills", destination: "/jobs" },
  // The briefing `/today` used to show is Home.
  { source: "/today", destination: "/" },
];
