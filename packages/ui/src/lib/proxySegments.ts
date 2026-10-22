// How the backend proxy (app/api/backend/[...path]) forwards each path
// segment. Kept apart from the route so the rule can be tested.

/** The segment to send upstream. Next.js hands the segments decoded, so an id
 *  the caller sent with `%2F` (a Google Chat chat, `google_chat:spaces/…`)
 *  arrives holding a slash and, joined back, would become several segments
 *  that route nowhere. It goes on as one opaque segment instead: `~` and its
 *  base64url, which the API reads back (`api/path_ids.py`). */
export function upstreamSegment(segment: string): string {
  if (segment.includes("/")) {
    return "~" + Buffer.from(segment, "utf8").toString("base64url");
  }
  return segment;
}

// API paths for this app's own server only, which the proxy never forwards:
// the store of sessions the identity provider ended (lib/endedSessionsServer.ts).
const SERVER_ONLY = ["auth/ended-sessions"];

/** Whether the proxy must refuse these segments: a server-only API path,
 *  or any `.` / `..` segment (which would otherwise resolve to one). Judge
 *  the path as the API will route it: resolved and percent-decoded. */
export function serverOnlyPath(segments: readonly string[]): boolean {
  if (segments.some((s) => s === "." || s === "..")) return true;
  const joined = segments.join("/").replace(/\/+$/, "").toLowerCase();
  return SERVER_ONLY.some((path) => joined === path || joined.startsWith(`${path}/`));
}
