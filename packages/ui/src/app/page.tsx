"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import ChatHome from "@/components/home/ChatHome";
import { legacyHomeRedirect } from "@/lib/chatRoutes";

// Home: the briefing, on the chat home (components/home/ChatHome.tsx). The
// old chat addresses, /?session=<id> and /?new=1 (with ?draft=, ?find=), go
// to /chats/<id> and /chats/new, so earlier links and bookmarks still work.
//
// Read from `window.location` rather than `useSearchParams`: that hook opts
// the page out of static rendering unless wrapped in <Suspense>, and Home is
// a heavy static page we want to keep prerendered.
export default function HomePage() {
  const router = useRouter();
  const [moving, setMoving] = useState<boolean | null>(null);

  useEffect(() => {
    const target = legacyHomeRedirect(window.location.search);
    if (target) router.replace(target);
    setMoving(target !== null);
  }, [router]);

  if (moving !== false) return <div className="h-full bg-surface" />;
  return <ChatHome start={{ kind: "briefing" }} />;
}
