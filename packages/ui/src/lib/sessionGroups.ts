// The Chats list's groups, as the API works them out (utils/chat_list.py):
// each chat's date group comes on it (`date_group`, GET /sessions?tz=), and
// the topic view's layout from POST /sessions/topics/layout. This only puts
// the chats the page already holds back under those headings; no rule of
// how to group lives here. Tested in scripts/sessionGroups.test.mjs.

import type { ChatTopic, SessionDateGroupKey, SessionSummary, TopicLayout } from "@/lib/api";

export type GroupKey = SessionDateGroupKey;

export interface SessionGroup {
  key: GroupKey;
  label: string;
  items: SessionSummary[];
}

/** The chats under their date groups, in the groups' order, each group in
 * the list's own order. A chat without a group (an older API) is Older. */
export function groupByDate(sessions: SessionSummary[]): SessionGroup[] {
  const groups = new Map<string, SessionGroup & { order: number }>();
  for (const s of sessions) {
    const g = s.date_group ?? { key: "older" as const, label: "Older", order: 5 };
    const group = groups.get(g.key) ?? { key: g.key, label: g.label, order: g.order, items: [] };
    group.items.push(s);
    groups.set(g.key, group);
  }
  return [...groups.values()]
    .sort((a, b) => a.order - b.order)
    .map(({ key, label, items }) => ({ key, label, items }));
}

export interface TopicGroup {
  /** "pinned", "topic:<id>" or "other". */
  key: string;
  label: string;
  /** The topic, for its menu; absent on Pinned and Other. */
  topic?: ChatTopic;
  items: SessionSummary[];
  /** When its newest chat was last active, in a word. */
  activeLabel?: string;
}

/** A heading in a band: one topic, or an area over two or more related ones. */
export interface TopicEntry {
  key: string;
  label: string;
  topic?: ChatTopic;
  area?: string;
  children: TopicGroup[];
  items: SessionSummary[];
  activeLabel?: string;
}

export interface HydratedLayout {
  pinned?: TopicGroup;
  bands: { key: string; label: string; entries: TopicEntry[] }[];
  other?: TopicGroup;
}

/** The server's topic layout with the page's chats and topics put back
 * under its ids. An id the page no longer holds is left out. */
export function hydrateTopicLayout(
  layout: TopicLayout,
  sessions: SessionSummary[],
  topics: ChatTopic[],
): HydratedLayout {
  const byId = new Map(sessions.map((s) => [s.session_id, s]));
  const topicById = new Map(topics.map((t) => [t.id, t]));
  const items = (ids: string[]) => ids.map((id) => byId.get(id)).filter((s): s is SessionSummary => s != null);
  const group = (g: TopicLayout["pinned"]): TopicGroup | undefined =>
    g
      ? {
          key: g.key,
          label: g.label,
          topic: g.topic_id != null ? topicById.get(g.topic_id) : undefined,
          items: items(g.session_ids),
          activeLabel: g.active_label,
        }
      : undefined;
  return {
    pinned: group(layout.pinned),
    other: group(layout.other),
    bands: layout.bands.map((band) => ({
      key: band.key,
      label: band.label,
      entries: band.entries.map((e) => ({
        key: e.key,
        label: e.label,
        topic: e.topic_id != null ? topicById.get(e.topic_id) : undefined,
        area: e.area,
        children: (e.children ?? []).map((c) => group(c)).filter((c): c is TopicGroup => c != null),
        items: items(e.session_ids),
        activeLabel: e.active_label,
      })),
    })),
  };
}
