"use client";

import type { EvidenceTable } from "@/lib/api";
import { evidenceCellText } from "@/lib/evidenceCells";

// What moved: the records behind an ERP alert, as ERPNext sent them
// (GET /today's `evidence`). Every cell is rendered as plain text.
export default function WhatMoved({ tables }: { tables: EvidenceTable[] }) {
  if (tables.length === 0) return null;
  return (
    <div className="mt-3 space-y-3">
      <div className="text-xs font-semibold uppercase tracking-wide text-fg-subtle">What moved</div>
      {tables.map((table, t) => (
        <div key={t} className="overflow-x-auto">
          <div className="mb-1 text-sm font-medium text-fg">{table.heading}</div>
          <table className="w-full text-sm text-fg-muted">
            <thead>
              <tr className="border-b border-line text-left text-xs text-fg-subtle">
                {table.columns.map((c) => (
                  <th
                    key={c.key}
                    scope="col"
                    className={`px-2 py-1 font-medium ${c.type === "text" || c.type === "doc" ? "" : "text-right"}`}
                  >
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, r) => (
                <tr key={r} className="border-b border-line/50 last:border-0">
                  {table.columns.map((c) => (
                    <td
                      key={c.key}
                      className={`px-2 py-1 break-words ${
                        c.type === "text" || c.type === "doc" ? "" : "text-right tabular-nums whitespace-nowrap"
                      }`}
                    >
                      {evidenceCellText(c.type, row[c.key], table.currency)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}
