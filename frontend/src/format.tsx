import type { RunEvent } from "./api";

// Integer paise in, rupee string out; no floating-point arithmetic on money.
export function formatPaise(paise: number): string {
  const rupees = Math.trunc(paise / 100).toLocaleString("en-IN");
  return `₹${rupees}.${String(paise % 100).padStart(2, "0")}`;
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  });
}

export const shortId = (id: string) => id.slice(0, 8);

// Run and step statuses, plus "outcome_unknown": not a backend status, but a
// failed run whose side-effecting step ran out of retries after its effect
// may have committed.
const LABELS: Record<string, string> = {
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  awaiting_approval: "Awaiting approval",
  rejected: "Rejected",
  outcome_unknown: "Outcome unknown",
  started: "In progress",
};

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge badge-${status}`}>{LABELS[status] ?? status}</span>;
}

export function outcomeUnknownEvent(events: RunEvent[]): RunEvent | undefined {
  return events.find((e) => e.type === "run_failed" && e.payload.outcome_unknown === true);
}
