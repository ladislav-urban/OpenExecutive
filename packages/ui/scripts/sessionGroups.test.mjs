import assert from "node:assert/strict";
import test from "node:test";
import { groupByDate, hydrateTopicLayout } from "../src/lib/sessionGroups.ts";

// The rules (which day a chat falls on, the topic bands, the areas) are the
// API's now: packages/core/tests/unit/test_chat_list.py. These check only
// that the page puts its chats back under the groups the API names.

const chat = (session_id, date_group, extra = {}) => ({
  session_id,
  title: session_id,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
  message_count: 1,
  ...(date_group ? { date_group } : {}),
  ...extra,
});
const g = (key, label, order) => ({ key, label, order });

test("chats sit under the date groups the API gave them, in the groups' order", () => {
  const groups = groupByDate([
    chat("y", g("yesterday", "Yesterday", 2)),
    chat("p", g("pinned", "Pinned", 0)),
    chat("t1", g("today", "Today", 1)),
    chat("t2", g("today", "Today", 1)),
    chat("legacy", null),
  ]);
  assert.deepEqual(
    groups.map((x) => [x.key, x.label, x.items.map((s) => s.session_id)]),
    [
      ["pinned", "Pinned", ["p"]],
      ["today", "Today", ["t1", "t2"]],
      ["yesterday", "Yesterday", ["y"]],
      // No group from the API (an older one): Older.
      ["older", "Older", ["legacy"]],
    ],
  );
});

test("the topic layout's ids become the page's chats and topics", () => {
  const sessions = [chat("a", null), chat("b", null), chat("c", null)];
  const topics = [{ id: 7, name: "Acme renewal", chat_count: 2, kept: true }];
  const layout = hydrateTopicLayout(
    {
      bands: [
        {
          key: "active",
          label: "Active this week",
          entries: [
            { key: "topic:7", label: "Acme renewal", topic_id: 7, session_ids: ["a", "b", "gone"], last_active: "x", active_label: "Today" },
          ],
        },
      ],
      other: { key: "other", label: "Other", session_ids: ["c"], active_label: "Tue" },
    },
    sessions,
    topics,
  );
  const [entry] = layout.bands[0].entries;
  assert.equal(entry.topic, topics[0]);
  // An id the page no longer holds is left out.
  assert.deepEqual(entry.items.map((s) => s.session_id), ["a", "b"]);
  assert.equal(entry.activeLabel, "Today");
  assert.deepEqual(entry.children, []);
  assert.deepEqual(layout.other.items.map((s) => s.session_id), ["c"]);
  assert.equal(layout.pinned, undefined);
});
