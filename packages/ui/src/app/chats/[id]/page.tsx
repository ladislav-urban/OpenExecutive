"use client";

import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import ChatHome from "@/components/home/ChatHome";

// One conversation at an address of its own, on the chat home
// (components/home/ChatHome.tsx). It opens only when it is one of the
// caller's own chats; anything else lands on Home. `?find=` names words to
// find in it (a search on /chats).
export default function ChatPage() {
  const params = useParams<{ id: string }>();
  const sessionId = params?.id ? decodeURIComponent(params.id) : "";
  const [find, setFind] = useState<string | null | undefined>(undefined);

  useEffect(() => {
    setFind(new URLSearchParams(window.location.search).get("find"));
  }, []);

  if (!sessionId || find === undefined) return <div className="h-full bg-surface" />;
  return <ChatHome start={{ kind: "chat", sessionId, find: find?.slice(0, 200) || undefined }} />;
}
