"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

import FrameConfirm from "@/components/embed/FrameConfirm";
import { CardActionNotConfirmed, frameActionFor, type CardAction } from "@/lib/cardActions";
import HOST_LIST from "@/lib/host-list.json";
import { DEFAULT_PRODUCT_NAME, withProduct } from "@/lib/productName";
import {
  acceptsEvent,
  embedRequested,
  parseHostMessage,
  readyMessage,
  themeProperties,
  type EmbedInit,
  type EmbedTheme,
} from "@/lib/embed";

// Embedded mode (lib/embed.ts): the page is in a host origin's frame, asked
// for with ?embed=1, and the host answered `ready` with a valid `init`. Until
// then, and in every other case, the page is exactly what it is outside a
// frame. AppShell reads `embedded` to leave out its own chrome.
//
// Apart from that, in ANY frame (embedded or not, since a page on a host
// origin can frame these pages without saying so), a card action, a chat
// message, and any other change off the host list that follows a click,
// waits for a Yes asked inside the frame that counts only once the browser
// reports it seen (lib/cardActions.ts, lib/seen.ts, FrameConfirm). A page
// that asks under the same rule itself (app/embed/approve) lets its one
// request through with `passOnce`.

const HOST_CALLS = HOST_LIST.calls.map(([method, pattern]) => [method, pattern] as const);
// A change made this soon after a click or a key in the frame may have been
// lured, so it asks; later than that, the page made it on its own.
const AFTER_INPUT_MS = 10_000;
// What counts as input: a click, a key, a drop or a paste (a page over the
// frame can lure any of them, and put its own file in a drag).
const INPUT_EVENTS = ["pointerdown", "keydown", "drop", "paste"] as const;

export type EmbedState = {
  embedded: boolean;
  init: EmbedInit | null;
  // The host origin that sent `init`; every later message must come from it.
  hostOrigin: string | null;
};

const NOT_EMBEDDED: EmbedState = { embedded: false, init: null, hostOrigin: null };
const EmbedContext = createContext<EmbedState>(NOT_EMBEDDED);

export function useEmbed(): EmbedState {
  return useContext(EmbedContext);
}

// The listed host origins (OE_HOST_ORIGINS), for a page a host opened or
// placed that answers it (app/embed/approve): empty when none is listed.
const HostOriginsContext = createContext<readonly string[]>([]);

export function useHostOrigins(): readonly string[] {
  return useContext(HostOriginsContext);
}

// What this app calls itself on its own pages (OE_PRODUCT_NAME, lib/productName.ts).
const ProductNameContext = createContext<string>(DEFAULT_PRODUCT_NAME);

export function useProductName(): string {
  return useContext(ProductNameContext);
}

/** Copy with the product's name for its PRODUCT token (lib/productName.ts). */
export function useNamed(): (text: string) => string {
  const product = useContext(ProductNameContext);
  return (text: string) => withProduct(text, product);
}

/** Whether this install is one product to the person: it has a name of its
 * own (OE_PRODUCT_NAME), or a host's frame gave it one. Then ERPNext is "the
 * ERP" to them (lib/erpnextDraftWords.ts), as in the Executive's replies. */
export function useErpNamed(): boolean {
  const product = useContext(ProductNameContext);
  const { init } = useContext(EmbedContext);
  return product !== DEFAULT_PRODUCT_NAME || Boolean(init?.productName);
}

/** Whether a request is the very one passed: same method, same address. */
function isPassed(pass: { method: string; url: string }, method: string, url: string): boolean {
  return pass.method === method.toUpperCase() && new URL(pass.url, location.href).href === new URL(url, location.href).href;
}

function applyTheme(theme: EmbedTheme, font: string | null): void {
  const root = document.documentElement;
  root.dataset.theme = theme.mode;
  root.style.colorScheme = theme.mode;
  for (const [name, value] of Object.entries(themeProperties(theme))) root.style.setProperty(name, value);
  if (font) {
    root.style.setProperty("--font-sans", font);
    root.style.setProperty("--font-display", font);
  }
}

type Pending = { action: CardAction; resolve: (ok: boolean) => void };

// How long a request let through with passOnce waits to be sent.
const PASS_ONCE_MS = 2_000;

// For a page whose own Yes was judged under lib/seen.ts's rule: the next
// request for exactly this card action goes through without asking again.
// Outside a frame there is nothing to pass, so it does nothing.
type PassOnce = (method: string, url: string) => void;
const PassOnceContext = createContext<PassOnce>(() => {});

export function usePassOnce(): PassOnce {
  return useContext(PassOnceContext);
}

export default function EmbedProvider({
  hostOrigins,
  productName,
  version,
  children,
}: {
  hostOrigins: string[];
  productName: string;
  version: string;
  children: React.ReactNode;
}) {
  const [state, setState] = useState<EmbedState>(NOT_EMBEDDED);
  const [pending, setPending] = useState<Pending | null>(null);
  const pendingRef = useRef<Pending | null>(null);
  const lastInput = useRef(0);
  const passing = useRef<{ method: string; url: string; until: number } | null>(null);

  // The handshake: `ready` to the parent, addressed to each listed host
  // origin (the browser delivers it only to the one the parent is), then
  // `init` and `theme` from that parent and origin alone.
  useEffect(() => {
    if (window.parent === window || hostOrigins.length === 0) return;
    if (!embedRequested(window.location.search)) return;
    const parent = window.parent;
    let lockedOrigin: string | null = null;
    let font: string | null = null;
    const onMessage = (event: MessageEvent) => {
      if (!acceptsEvent(event, lockedOrigin ? [lockedOrigin] : hostOrigins, parent)) return;
      const message = parseHostMessage(event.data);
      if (!message) return;
      if (message.type === "init") {
        // The way back must lead to the very host that framed this page.
        const { returnBase, ...init } = message.init;
        if (returnBase && new URL(returnBase).origin !== event.origin) return;
        lockedOrigin = event.origin;
        font = init.font;
        applyTheme(init.theme, font);
        setState({ embedded: true, init: returnBase ? { ...init, returnBase } : init, hostOrigin: event.origin });
      } else if (lockedOrigin) {
        applyTheme(message.theme, font);
        setState((prev) => (prev.init ? { ...prev, init: { ...prev.init, theme: message.theme } } : prev));
      }
    };
    window.addEventListener("message", onMessage);
    for (const origin of hostOrigins) parent.postMessage(readyMessage(version), origin);
    return () => window.removeEventListener("message", onMessage);
  }, [hostOrigins, version]);

  // In any frame: every card action, and any change off the host list that
  // follows a click, waits for a Yes in FrameConfirm.
  useEffect(() => {
    if (window.top === window.self) return;
    const onInput = (event: Event) => {
      if (event.isTrusted) lastInput.current = Date.now();
    };
    for (const type of INPUT_EVENTS) window.addEventListener(type, onInput, true);
    const original = window.fetch;
    const confirm = (action: CardAction) =>
      new Promise<boolean>((resolve) => {
        // One at a time: a second action while one is asked about is refused.
        if (pendingRef.current) return resolve(false);
        const next = { action, resolve };
        pendingRef.current = next;
        setPending(next);
      });
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const method = init?.method ?? (input instanceof Request ? input.method : "GET");
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const action = frameActionFor(method, url, init?.body, HOST_CALLS);
      // A pass is used up by the next change, whichever it is; reads leave it.
      const pass = action ? passing.current : null;
      if (action) passing.current = null;
      if (action && pass && Date.now() <= pass.until && isPassed(pass, method, url)) return original(input, init);
      const asks = action !== null && (action.card || Date.now() - lastInput.current < AFTER_INPUT_MS);
      if (asks && !(await confirm(action))) throw new CardActionNotConfirmed();
      return original(input, init);
    };
    return () => {
      window.fetch = original;
      for (const type of INPUT_EVENTS) window.removeEventListener(type, onInput, true);
    };
  }, []);

  const passOnce = useCallback<PassOnce>((method, url) => {
    passing.current = { method: method.toUpperCase(), url, until: Date.now() + PASS_ONCE_MS };
  }, []);

  const answer = (ok: boolean) => {
    const current = pendingRef.current;
    pendingRef.current = null;
    // A Yes is input too: what the confirmed change sets off next asks again.
    lastInput.current = Date.now();
    setPending(null);
    current?.resolve(ok);
  };

  return (
    <HostOriginsContext.Provider value={hostOrigins}>
      <ProductNameContext.Provider value={productName}>
      <PassOnceContext.Provider value={passOnce}>
        <EmbedContext.Provider value={state}>
          {children}
          {pending && (
            <FrameConfirm
              action={pending.action}
              onAnswer={answer}
              productName={productName}
              returnBase={state.init?.returnBase ?? null}
              hostOrigins={hostOrigins}
            />
          )}
        </EmbedContext.Provider>
      </PassOnceContext.Provider>
      </ProductNameContext.Provider>
    </HostOriginsContext.Provider>
  );
}
