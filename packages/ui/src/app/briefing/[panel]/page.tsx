"use client";

import { notFound, useParams, useRouter } from "next/navigation";
import { useCallback } from "react";

import Briefing from "@/components/Briefing";
import { leaveHandoff } from "@/components/home/handoff";
import { usePageCrumb } from "@/components/shell/PageCrumb";
import { NEW_CHAT_PATH, isBriefingPanel } from "@/lib/chatRoutes";

const TITLES: Record<string, string> = {
  brief: "Today's brief",
  departments: "Departments",
  people: "People",
  team: "Across the team",
  inFlight: "Under way",
  handled: "Handled",
  monitoring: "Monitoring",
  clients: "Across your clients",
};

// One of Home's panels on a page of its own (/briefing/<panel>, the keys in
// lib/chatRoutes.ts's BRIEFING_PANELS): what a host showing Home natively
// opens in its frame when a tile is clicked. The same body and footer links
// as Home's side panel; a hand-off to chat (Discuss, Nudge) opens a new chat
// that sends it, as it would from Home.
export default function BriefingPanelPage() {
  const params = useParams<{ panel: string }>();
  const panel = params?.panel ?? "";
  const router = useRouter();
  usePageCrumb(TITLES[panel] ?? "Home");

  const onContinue = useCallback(
    (prompt: string, memoryText?: string) => {
      leaveHandoff({ prompt, memoryText });
      router.push(NEW_CHAT_PATH);
    },
    [router],
  );

  if (!isBriefingPanel(panel)) notFound();
  return <Briefing panel={panel} onContinue={onContinue} />;
}
