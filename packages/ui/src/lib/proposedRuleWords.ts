// What a proposed bank rule says on Home (components/ProposedRules.tsx).
// Kept free of runtime imports so scripts/proposedRuleWords.test.mjs runs it
// under plain Node.
import type { ProposedRule } from "./api";

const DIRECTION: Record<string, string> = {
  Withdrawal: "money going out",
  Deposit: "money coming in",
  Any: "any bank line",
};

/** One sentence: which lines the rule tags, as what, and on how much evidence. */
export function proposedRuleLine(rule: Pick<ProposedRule, "word" | "account" | "transaction_type" | "lines" | "misses">): string {
  const which = DIRECTION[rule.transaction_type] ?? "bank lines";
  const seen = rule.lines === 1 ? "1 reconciled line" : `${rule.lines} reconciled lines`;
  const missed = rule.misses === 0 ? ", none of them booked elsewhere" : `, ${rule.misses} booked elsewhere`;
  return `Tag ${which} whose description contains “${rule.word}” as ${rule.account}. Seen in ${seen}${missed}.`;
}
