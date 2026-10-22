// A link to an ERPNext document (a made draft's "Open in ERPNext"): in a
// host's frame, opened in the host's own desk; anywhere else, a new tab.
//
// In embedded mode (components/embed/EmbedProvider.tsx) the frame's host is
// the origin its `init` came from. A link on exactly that origin is the
// host's own page, so the frame asks the host to open it
// (`open {kind: "host", route}`, lib/embed.ts) rather than open a second
// ERPNext in a tab. Only a desk route goes: `/desk/...` or `/app/...`, as the
// host itself requires (oe_connector's oe_embed.bundle.js, DESK_ROUTE_RE),
// so a route the host would refuse stays a plain link. Any other origin, or
// outside embedded mode, keeps the link as it was.
//
// No imports, so `npm test` can exercise this.

export type ErpnextLink = { kind: "host"; route: string } | { kind: "tab"; href: string };

// The host's own rule, kept in step with oe_embed.bundle.js's DESK_ROUTE_RE.
const DESK_ROUTE_RE = /^\/(?:desk|app)\/[A-Za-z0-9_\-./%]*(?:\?[^#\s]*)?$/;

/** How to open `href` (an absolute http(s) URL): in the host, or in a new tab. */
export function erpnextLink(href: string, hostOrigin: string | null): ErpnextLink {
  const tab: ErpnextLink = { kind: "tab", href };
  if (!hostOrigin) return tab;
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return tab;
  }
  if (url.origin !== hostOrigin || url.username || url.password) return tab;
  const route = `${url.pathname}${url.search}`;
  if (!DESK_ROUTE_RE.test(route) || route.includes("..") || route.includes("//")) return tab;
  return { kind: "host", route };
}
