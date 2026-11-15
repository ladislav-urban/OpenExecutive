"use client";

import { useEffect, useState } from "react";

import { useEmbed } from "@/components/embed/EmbedProvider";
import { getProposedRules, type ProposedRule } from "@/lib/api";
import { openMessage } from "@/lib/embed";
import { erpnextLink } from "@/lib/erpnextLinks";
import { formatRelativeTime } from "@/lib/relativeTime";
import { proposedRuleLine } from "@/lib/proposedRuleWords";

// Home: the bank rules the weekly bank-rules job proposed, waiting for a
// System Manager to enable or dismiss in the desk. GET /erpnext/proposed-rules
// lists them for the principal alone, read as their own user, so only a
// System Manager sees any. Nothing is decided here: each links to its
// proposal. The rule's word comes from bank lines, so it is shown as plain
// text and never as markup.

export function useProposedRules(): ProposedRule[] {
  const [rules, setRules] = useState<ProposedRule[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    getProposedRules(controller.signal)
      .then(setRules)
      .catch(() => setRules([]));
    return () => controller.abort();
  }, []);
  return rules;
}

export function ProposedRuleItem({ rule, emphasized = false }: { rule: ProposedRule; emphasized?: boolean }) {
  const { hostOrigin } = useEmbed();
  const link = rule.url && /^https?:\/\//.test(rule.url) ? rule.url : null;
  const opens = link ? erpnextLink(link, hostOrigin) : null;
  const openInHost = (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (opens?.kind !== "host" || !hostOrigin) return;
    const message = openMessage({ kind: "host", route: opens.route });
    if (!message) return;
    event.preventDefault();
    window.parent.postMessage(message, hostOrigin);
  };
  return (
    <article
      className={`rounded-2xl border bg-surface-elevated p-4 sm:p-5 ${
        emphasized ? "border-accent/60 ring-1 ring-accent/25 shadow-sm" : "border-line"
      }`}
    >
      <div className="mb-2 flex items-center gap-2">
        <span className="inline-flex items-center rounded-lg bg-surface-overlay px-2 py-0.5 text-[13px] font-medium text-fg-muted">
          To decide
        </span>
        <span className="text-[12px] font-medium text-fg-muted">Bank rule proposed</span>
        {rule.proposed && (
          <span className="text-[12px] text-fg-subtle">{formatRelativeTime(rule.proposed)}</span>
        )}
      </div>
      <p className="text-sm text-fg">{proposedRuleLine(rule)}</p>
      <p className="mt-1 text-[13px] text-fg-muted">
        {rule.company} · enabling it only tags lines; each entry is still made by a person.
      </p>
      {link && (
        <div className="mt-3">
          <a
            href={link}
            target="_blank"
            rel="noopener noreferrer"
            onClick={openInHost}
            className="text-sm font-medium text-accent underline"
          >
            Review the proposal
          </a>
        </div>
      )}
    </article>
  );
}
