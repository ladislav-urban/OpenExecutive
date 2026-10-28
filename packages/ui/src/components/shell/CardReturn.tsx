"use client";

import { useSyncExternalStore } from "react";

import { useHostOrigins, useProductName } from "@/components/embed/EmbedProvider";
import { returnFromQuery } from "@/lib/signinReturn";

// A card's own page opened from a host's frame (components/embed/FrameConfirm.tsx)
// carries the way back to the host's page for it in `?return=`, checked
// against the host origins (lib/signinReturn.ts). Such a page is part of the
// host's product for the person, so it shows bare: the card, this app's
// product name (OE_PRODUCT_NAME) and a link back, without this app's own
// navigation. The link is only offered, never followed on its own.

const CARD_PAGE_RE = /^\/cards\/(?:alert\/)?\d+\/?$/;

export function isCardPage(pathname: string): boolean {
  return CARD_PAGE_RE.test(pathname);
}

const never = () => () => {};

/**
 * The way back for a card page: the host page, null when there is none, and
 * undefined before the browser's address can be read (the server's render,
 * and the first one in the browser), so neither layout flashes for the other.
 */
export function useCardReturn(pathname: string): string | null | undefined {
  const origins = useHostOrigins();
  // Read from `window.location` rather than `useSearchParams`, as Home does:
  // the hook needs a Suspense boundary around the whole shell.
  const search = useSyncExternalStore(never, () => window.location.search, () => null);
  if (!isCardPage(pathname)) return null;
  if (search === null) return undefined;
  return returnFromQuery(search, origins);
}

/** The host's name for a way back: its address's host name. */
export function returnHostName(to: string): string {
  return new URL(to).hostname;
}

export function BackToHost({ to, className = "" }: { to: string; className?: string }) {
  return (
    <a href={to} className={`text-accent hover:underline ${className}`}>
      Back to {returnHostName(to)}
    </a>
  );
}

export default function CardReturnShell({ to, children }: { to: string | undefined; children: React.ReactNode }) {
  const name = useProductName();
  return (
    <div className="flex h-full flex-col bg-surface text-fg">
      <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3 sm:px-6">
        <span className="font-display text-base font-extrabold tracking-tight">{name}</span>
        {to && <BackToHost to={to} className="text-sm" />}
      </header>
      <div className="flex-1 min-h-0 overflow-y-auto">{children}</div>
    </div>
  );
}
