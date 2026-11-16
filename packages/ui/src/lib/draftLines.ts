// The table under a draft card's fields, for every kind of card: what its
// lines are (a Material Request's, Quotation's or Purchase Order's items, an
// invoice fill's rows, a bank match's vouchers) decides the columns. Kept
// apart from the card so it can be tested (scripts/draftLines.test.mjs).
// Every value is ERPNext's own, shown as plain text.

import type { ErpnextDraftLine } from "./api";

export interface LineColumn {
  key: keyof ErpnextDraftLine;
  label: string;
  numeric?: boolean;
}

export interface LineTable {
  columns: LineColumn[];
  rows: string[][];
}

const VOUCHER: LineColumn[] = [
  { key: "voucher", label: "Voucher" },
  { key: "dated", label: "Dated" },
  { key: "party", label: "Party" },
  { key: "allocated", label: "Allocated", numeric: true },
  { key: "clears_on", label: "Clears on" },
];

const FILL: LineColumn[] = [
  { key: "row", label: "Row", numeric: true },
  { key: "item", label: "Item" },
  { key: "qty", label: "Qty", numeric: true },
  { key: "rate", label: "Rate", numeric: true },
  { key: "amount", label: "Amount", numeric: true },
];

function value(line: ErpnextDraftLine, key: keyof ErpnextDraftLine): string {
  const v = line[key];
  return typeof v === "string" ? v : "";
}

const has = (lines: ErpnextDraftLine[], key: keyof ErpnextDraftLine) => lines.some((l) => Boolean(value(l, key)));

/** The columns and cells of a card's lines, or null when it has none. */
export function lineTable(lines: ErpnextDraftLine[]): LineTable | null {
  if (lines.length === 0) return null;
  let columns: LineColumn[];
  if (has(lines, "voucher")) {
    columns = VOUCHER;
  } else if (has(lines, "row") || has(lines, "item")) {
    columns = FILL;
  } else {
    // A Quotation's or Purchase Order's lines carry ERPNext's rates; a
    // Quotation's may name no warehouse.
    const priced = has(lines, "rate") || has(lines, "amount");
    columns = [
      { key: "item_code", label: "Item" },
      { key: "qty", label: "Qty", numeric: true },
      ...(priced
        ? [
            { key: "rate", label: "Rate", numeric: true } as LineColumn,
            { key: "amount", label: "Amount", numeric: true } as LineColumn,
          ]
        : []),
      ...(has(lines, "warehouse") ? [{ key: "warehouse", label: "Warehouse" } as LineColumn] : []),
    ];
  }
  const priced = columns.some((c) => c.key === "rate");
  return {
    columns,
    rows: lines.map((line) =>
      columns.map((c) => value(line, c.key) || (priced && (c.key === "rate" || c.key === "amount") ? "—" : "")),
    ),
  };
}
