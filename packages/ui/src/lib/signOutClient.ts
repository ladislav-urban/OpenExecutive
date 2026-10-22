"use client";

// Sign out, from the account menu. An SSO session ends at the identity
// provider too (app/api/auth/end-session): a top-level form post, so the
// browser follows the provider's redirects and comes back to /signin. Any
// other session (Google, local login) signs out here alone, as before.

import type { Session } from "next-auth";
import { signOut } from "next-auth/react";

export function signOutOf(session: Session | null): void {
  if (!session?.providerSession) {
    void signOut({ callbackUrl: "/signin" });
    return;
  }
  const form = document.createElement("form");
  form.method = "POST";
  form.action = "/api/auth/end-session";
  document.body.appendChild(form);
  form.submit();
}
