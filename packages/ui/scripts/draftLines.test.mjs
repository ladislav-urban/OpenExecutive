import assert from "node:assert/strict";
import test from "node:test";
import { lineTable } from "../src/lib/draftLines.ts";

// The lines each kind of card carries, as GET /erpnext/drafts lists them.
const MATCH = [{
  voucher: "Payment Entry ACC-PAY-2026-00042", dated: "2026-11-13", party: "Metro Transit Works",
  allocated: "1,200.00 USD", clears_on: "2026-11-14", matched_elsewhere: "0", created_by: "accounts@example.com",
}];
const FILL = [{ row: "1", item: "SENSOR-01", qty: "10", rate: "12.50 USD", amount: "125.00 USD" }];
const REQUEST = [{ item_code: "SENSOR-01", qty: "150", warehouse: "Stores - DC" }];
const QUOTE = [{ item_code: "SENSOR-01", qty: "150", warehouse: "", rate: "12.50", amount: "" }];

const labels = (t) => t.columns.map((c) => c.label);

test("a bank-match card shows its vouchers", () => {
  const t = lineTable(MATCH);
  assert.deepEqual(labels(t), ["Voucher", "Dated", "Party", "Allocated", "Clears on"]);
  assert.deepEqual(t.rows, [["Payment Entry ACC-PAY-2026-00042", "2026-11-13", "Metro Transit Works", "1,200.00 USD", "2026-11-14"]]);
});

test("an invoice-fill card shows its rows", () => {
  const t = lineTable(FILL);
  assert.deepEqual(labels(t), ["Row", "Item", "Qty", "Rate", "Amount"]);
  assert.deepEqual(t.rows, [["1", "SENSOR-01", "10", "12.50 USD", "125.00 USD"]]);
});

test("a Material Request shows its items and warehouse, unpriced", () => {
  const t = lineTable(REQUEST);
  assert.deepEqual(labels(t), ["Item", "Qty", "Warehouse"]);
  assert.deepEqual(t.rows, [["SENSOR-01", "150", "Stores - DC"]]);
});

test("a Quotation's missing amount reads as a dash, and no warehouse column", () => {
  const t = lineTable(QUOTE);
  assert.deepEqual(labels(t), ["Item", "Qty", "Rate", "Amount"]);
  assert.deepEqual(t.rows, [["SENSOR-01", "150", "12.50", "—"]]);
});

test("a card with no lines has no table", () => {
  assert.equal(lineTable([]), null);
});
