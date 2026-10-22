"use client";

import { useEffect, useState } from "react";

import ChatHome from "@/components/home/ChatHome";

// A new chat at an address of its own, on the chat home
// (components/home/ChatHome.tsx). `?draft=` pre-fills its input without
// sending it (the text came from a link, not from the person).
export default function NewChatPage() {
  const [draft, setDraft] = useState<string | null | undefined>(undefined);

  useEffect(() => {
    setDraft(new URLSearchParams(window.location.search).get("draft"));
  }, []);

  if (draft === undefined) return <div className="h-full bg-surface" />;
  return <ChatHome start={{ kind: "new", draft: draft ?? undefined }} />;
}
