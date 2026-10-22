// Local login (see lib/localLogin.ts): the `jwt` callback in auth.ts marks
// the token at sign-in and `session` copies the mark here. Such a session has
// no email by design.
//
// An SSO session (lib/oidc.ts) also remembers the provider session it stands
// on (`providerSession`: the ID token's `iss`, `sid` and `sub`, and when it
// began), which every request checks against the sessions the provider ended
// (lib/endedSessions.ts). The ID token itself stays in the encrypted JWT, for
// signing out at the provider; it never reaches the page.
import "next-auth";
import "next-auth/jwt";

import type { ProviderSession } from "@/lib/endedSessions";

declare module "next-auth" {
  interface Session {
    localLogin?: boolean;
    providerSession?: ProviderSession;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    localLogin?: boolean;
    providerSession?: ProviderSession;
    idToken?: string;
  }
}
