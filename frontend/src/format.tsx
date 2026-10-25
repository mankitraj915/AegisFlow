import type { Run, RunEvent } from "./api";

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

// An identifier like check_refund_policy that may wrap only after its underscores.
export function Ident({ children }: { children: string }) {
  return <>{children.split("_").flatMap((part, i) => (i ? ["_", <wbr key={i} />, part] : [part]))}</>;
}

// What each pipeline step does, from backend/aegisflow/tools.py.
export const STEP_INFO: Record<string, { title: string; does: string }> = {
  fetch_order: { title: "Fetch order", does: "Loads the order from the mock order data." },
  fetch_customer: { title: "Fetch customer", does: "Looks up the order's customer." },
  check_refund_policy: { title: "Check policy", does: "Only delivered orders are refundable." },
  propose_refund: { title: "Propose refund", does: "Proposes a full refund of the order total." },
  issue_refund: { title: "Issue refund", does: "Writes the refund to the simulated payment ledger." },
  notify_customer: { title: "Notify customer", does: "Composes the customer email. Nothing is sent." },
};

export type Tone = "ok" | "run" | "gate" | "cut" | "fail" | "unknown" | "idle";

// Run statuses, plus two derived from events rather than the backend status:
// "outcome_unknown", a failed run whose side-effecting step ran out of retries
// after its effect may have committed, and "interrupted", a running run
// stopped by a simulated interruption that has not been resumed.
const RUN_STATUS: Record<string, { label: string; tone: Tone }> = {
  running: { label: "Running", tone: "run" },
  succeeded: { label: "Succeeded", tone: "ok" },
  failed: { label: "Failed", tone: "fail" },
  awaiting_approval: { label: "Awaiting approval", tone: "gate" },
  rejected: { label: "Rejected", tone: "fail" },
  outcome_unknown: { label: "Outcome unknown", tone: "unknown" },
  interrupted: { label: "Interrupted", tone: "cut" },
};

export function displayStatus(run: Run, outcomeUnknown = false): string {
  if (outcomeUnknown) return "outcome_unknown";
  return run.interrupted ? "interrupted" : run.status;
}

export function StatusMark({ status }: { status: string }) {
  const { label, tone } = RUN_STATUS[status] ?? { label: status, tone: "idle" };
  return (
    <span className={`status tone-${tone}`}>
      <span className="glyph" aria-hidden="true" />
      {label}
    </span>
  );
}

export function outcomeUnknownEvent(events: RunEvent[]): RunEvent | undefined {
  return events.find((e) => e.type === "run_failed" && e.payload.outcome_unknown === true);
}
