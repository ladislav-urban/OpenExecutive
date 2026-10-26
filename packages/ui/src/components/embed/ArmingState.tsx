"use client";

// What an in-frame Yes shows while it arms (lib/seen.ts): a bar that fills as
// the box is seen and the pointer rests on it, then "Ready" once a single
// click counts. Display only: the gate alone decides whether a press counts,
// and a press made before "Ready" never counts later.

/** The ring a Yes button wears once it is ready, beside `buttonClass`. */
export const READY_RING = "ring-2 ring-emerald-500 ring-offset-2 ring-offset-surface";

export default function ArmingState({
  progress,
  ready,
  does,
}: {
  progress: number;
  ready: boolean;
  /** What the one click does, after "one click": "approves", "rejects it"… */
  does: string;
}) {
  const filled = Math.round((ready ? 1 : Math.max(0, Math.min(1, progress))) * 100);
  return (
    <div className="space-y-1" aria-live="polite">
      <div className="h-1 w-full overflow-hidden rounded-full bg-line" aria-hidden="true">
        <div
          className={`h-full rounded-full transition-[width] duration-100 ${ready ? "bg-emerald-500" : "bg-accent"}`}
          style={{ width: `${filled}%` }}
        />
      </div>
      {/* One line of fixed height in every state: the inline Approve tells
          its host its height, so a line that wrapped differently when ready
          would tell the host the moment it armed. */}
      <p
        className={`h-4 truncate text-xs leading-4 ${ready ? "font-medium text-emerald-700 dark:text-emerald-300" : "text-fg-subtle"}`}
      >
        {ready
          ? `Ready: one click ${does}.`
          : filled > 0
            ? "Getting ready… keep the pointer on this box."
            : "Rest the pointer on this box to get ready."}
      </p>
    </div>
  );
}
