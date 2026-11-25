import HOST_LIST from "@/lib/host-list.json";
import { parseHostOrigins, type HostCall, type HostOrigins } from "@/lib/hostOrigins";
import { productName } from "@/lib/productName";
import { parseDeskBase } from "@/lib/deskRedirect";
import { parseReturnBase } from "@/lib/embed";

// Server-only: OE_HOST_ORIGINS, OE_PRODUCT_NAME and the host list, read once at startup (a
// restart makes an edit live, as for ALLOWED_EMAILS). Unset, there is no host
// origin and every page may be framed by this app alone.

export const HOST_ORIGINS: HostOrigins = parseHostOrigins(process.env.OE_HOST_ORIGINS);
if (HOST_ORIGINS.error) console.error(`[host origins] ${HOST_ORIGINS.error}; no host origin is trusted`);

export const HOST_CALLS: readonly HostCall[] = HOST_LIST.calls.map(([method, pattern]) => [method, pattern] as const);

// The desk route that frames this app's pages (OE_DESK_BASE, lib/deskRedirect.ts):
// with it, a direct visit to a page goes to the same screen there. Unset or
// invalid, nothing is redirected.
const DESK = parseDeskBase(process.env.OE_DESK_BASE, HOST_ORIGINS.origins, parseReturnBase);
if (DESK.error) console.error(`[desk base] ${DESK.error}; direct visits stay here`);
export const DESK_BASE: string | null = DESK.base;

// What this app calls itself on its own pages (lib/productName.ts).
export const PRODUCT_NAME: string = productName(process.env.OE_PRODUCT_NAME);
