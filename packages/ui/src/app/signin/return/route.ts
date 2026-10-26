import { cookies } from "next/headers";

import { HOST_ORIGINS } from "@/lib/hostConfig";
import { RETURN_COOKIE, RETURN_COOKIE_PATH, returnTarget } from "@/lib/signinReturn";

// The way back from /signin/sso once signed in: `to` is checked again here
// (lib/signinReturn.ts), never trusted across the hop through the provider,
// and the tab goes to that host page, or to this app's Home. Behind the
// session check like any page: without a session the middleware sends the
// tab to /signin first.

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  (await cookies()).delete({ name: RETURN_COOKIE, path: RETURN_COOKIE_PATH });
  const to = returnTarget(new URL(request.url).searchParams.get("to"), HOST_ORIGINS.origins);
  return Response.redirect(to ?? new URL("/", process.env.AUTH_URL?.trim() || request.url));
}
