"use client";

import { useEffect, useState } from "react";

import { getPeopleViewer } from "@/lib/api";
import { downloadOversightCsv, oversightCsvUrl } from "@/lib/oversight";
import { buttonClass } from "@/components/ui/Button";
import { useErpNamed } from "@/components/embed/EmbedProvider";
import { erpName } from "@/lib/erpnextDraftWords";

// The audit log's "Download oversight record (CSV)", for the principal only:
// the record names everyone who asked for an ERPNext draft, so the route
// refuses anyone else, and the page doesn't offer it to them either.
export default function OversightExport() {
  const named = useErpNamed();
  const [principal, setPrincipal] = useState(false);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    getPeopleViewer()
      .then((v) => {
        if (live) setPrincipal(v.is_principal === true);
      })
      .catch(() => {
        if (live) setPrincipal(false);
      });
    return () => {
      live = false;
    };
  }, []);

  if (!principal) return null;

  const valid = oversightCsvUrl(from, to) !== null;

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      await downloadOversightCsv(from, to);
    } catch (e) {
      setError(e instanceof Error ? e.message : "The export failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      aria-labelledby="oversight-heading"
      className="mb-6 rounded-xl border border-line bg-surface-elevated px-4 py-3"
    >
      <h2 id="oversight-heading" className="text-[15px] font-semibold text-fg">
        Draft oversight record
      </h2>
      <p className="mt-1 text-sm text-fg-muted">
        Every draft card in the range: who asked, what it showed, who decided, and what
        became of it in {erpName(named)}. Leave a date empty for an open end.
      </p>
      <div className="mt-3 flex flex-wrap items-end gap-3">
        <label className="text-xs text-fg-muted flex flex-col gap-1">
          From
          <input
            type="date"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            className="px-2 py-1 rounded-lg bg-surface border border-line text-sm focus:outline-none focus:border-indigo-500"
          />
        </label>
        <label className="text-xs text-fg-muted flex flex-col gap-1">
          To
          <input
            type="date"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            className="px-2 py-1 rounded-lg bg-surface border border-line text-sm focus:outline-none focus:border-indigo-500"
          />
        </label>
        <button
          type="button"
          onClick={download}
          disabled={busy || !valid}
          className={buttonClass("secondary", "sm")}
        >
          {busy ? "Preparing…" : "Download oversight record (CSV)"}
        </button>
      </div>
      {!valid && <p className="mt-2 text-sm text-fg-muted">Choose a start date on or before the end date.</p>}
      {error && (
        <p role="alert" className="mt-2 text-sm text-rose-400">
          {error}
        </p>
      )}
    </section>
  );
}
