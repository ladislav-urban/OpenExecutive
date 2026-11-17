"use client";

import { useProductName } from "@/components/embed/EmbedProvider";
import SettingsSubpage from "@/components/settings/SettingsSubpage";
import WorkspaceCard from "@/components/settings/WorkspaceCard";

// Settings → Workspace: who the product is for, and when it acts.
export default function WorkspaceSettingsPage() {
  const product = useProductName();
  return (
    <SettingsSubpage
      title="Workspace"
      description={`Who ${product} is for and the time zone your briefs run in.`}
    >
      <WorkspaceCard />
    </SettingsSubpage>
  );
}
