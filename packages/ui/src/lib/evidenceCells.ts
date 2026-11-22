// What moved: one cell of an ERP alert's evidence table as the text it shows
// (integrations/erpnext_evidence.py). Every cell is ERPNext's data, partly
// written by outsiders, so it is only ever text: never Markdown or HTML.

export type EvidenceCellType = "text" | "money" | "number" | "percent" | "date" | "doc";

export type EvidenceCellValue = string | number | { doctype: string; name: string } | null | undefined;

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** The text one cell shows. A `money` cell is a signed number in the table's
 * currency (a bare number when it has none); a `percent` cell is already in
 * points (12.5 is 12.5%); a `date` is an ISO day, shown in the reader's
 * locale; a `doc` is its DocType and name. An empty or unexpected cell shows
 * a dash. */
export function evidenceCellText(
  type: EvidenceCellType,
  value: EvidenceCellValue,
  currency: string | null,
  locale?: string,
): string {
  if (value === null || value === undefined) return "–";
  if (type === "doc") {
    return typeof value === "object" ? `${value.doctype} ${value.name}` : "–";
  }
  if (type === "money" || type === "number" || type === "percent") {
    if (typeof value !== "number" || !Number.isFinite(value)) return "–";
    if (type === "percent") {
      return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(value)}%`;
    }
    if (type === "money" && currency) {
      try {
        return new Intl.NumberFormat(locale, { style: "currency", currency }).format(value);
      } catch {
        // An unknown currency code: the number and the code.
        return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(value)} ${currency}`;
      }
    }
    return new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(value);
  }
  if (typeof value !== "string") return "–";
  if (type === "date") {
    const m = DATE_RE.exec(value);
    if (!m) return value;
    // The day itself, not a UTC midnight shifted into the reader's zone.
    const day = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return new Intl.DateTimeFormat(locale, { year: "numeric", month: "short", day: "numeric" }).format(day);
  }
  return value;
}
