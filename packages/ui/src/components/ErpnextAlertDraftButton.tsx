"use client";

import { useEffect, useState, useSyncExternalStore } from "react";

import { reloadErpnextDraftCards } from "@/components/ErpnextDraftCards";
import { useErpNamed } from "@/components/embed/EmbedProvider";
import { erpName } from "@/lib/erpnextDraftWords";
import Button from "@/components/ui/Button";
import { getErpnextAlertDrafts, prepareErpnextAlertDraft, type ErpnextAlertDraft } from "@/lib/api";

// Today: the "Prepare restock" / "Prepare follow-up" button on an ERPNext
// alert that suggests a draft. Tapping it has ERPNext check the draft as you
// and leaves its card in Needs you, to approve like any other; nothing is in
// ERPNext until then. GET /erpnext/alert-drafts answers the principal only,
// so nobody else sees a button.
//
// One request for every alert card on screen.

let suggestions: ErpnextAlertDraft[] = [];
const listeners = new Set<() => void>();
let loaded: Promise<void> | null = null;

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

const read = () => suggestions;
const readOnServer = (): ErpnextAlertDraft[] => [];

function load(): Promise<void> {
  if (!loaded) {
    loaded = getErpnextAlertDrafts()
      .then((fresh) => {
        suggestions = fresh;
        for (const listener of listeners) listener();
      })
      .catch(() => { /* no button is better than a broken one */ });
  }
  return loaded;
}

export default function ErpnextAlertDraftButton({ alertId }: { alertId: number }) {
  const all = useSyncExternalStore(subscribe, read, readOnServer);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const erp = erpName(useErpNamed());
  useEffect(() => {
    void load();
  }, []);
  const suggestion = all.find((s) => s.alert_id === alertId);
  if (!suggestion) return null;

  const prepare = async () => {
    setBusy(true);
    setError(null);
    try {
      const prepared = await prepareErpnextAlertDraft(alertId);
      setDone(
        prepared.already_waiting
          ? "Its draft card is already waiting in Your decision."
          : `Its draft card is in Your decision. Nothing is in ${erp} until you approve it.`,
      );
      void reloadErpnextDraftCards();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't prepare that draft.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      {done ? (
        <p className="text-sm text-emerald-700 dark:text-emerald-300">{done}</p>
      ) : (
        <Button variant="secondary" onClick={prepare} disabled={busy}>
          {busy ? `Asking ${erp}…` : suggestion.label}
        </Button>
      )}
      {error && <p className="text-sm text-red-600 dark:text-red-400 break-words">{error}</p>}
    </div>
  );
}
