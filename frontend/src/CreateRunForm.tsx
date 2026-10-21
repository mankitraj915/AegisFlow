import { useState, type FormEvent } from "react";
import { api, messageOf, PIPELINE, type FaultKind, type Run } from "./api";

// Mock orders seeded in backend/aegisflow/tools.py (ORDERS). Suggestions only;
// any id is sent and the backend answers 404 for unknown orders.
const DEMO_ORDERS = [
  ["ORD-1001", "₹1,299 · under default threshold"],
  ["ORD-2002", "₹12,000 · over default threshold"],
  ["ORD-3003", "₹499 · flagged for manual review"],
  ["ORD-4004", "₹5,000 · exactly at default threshold"],
  ["ORD-9009", "cancelled · not refundable"],
];

// Per-attempt fault plans. The engine retries up to 3 attempts in total.
const FAULT_PRESETS: Record<string, { label: string; plan: FaultKind[] }> = {
  none: { label: "No fault", plan: [] },
  transient: { label: "Fails once, then succeeds", plan: ["retryable"] },
  exhaust: { label: "Fails on every attempt", plan: ["retryable", "retryable", "retryable"] },
  fatal: { label: "Fatal error", plan: ["fatal"] },
  commit_once: { label: "Times out after commit, once", plan: ["retryable_after_commit"] },
  commit_exhaust: {
    label: "Times out after commit, every attempt",
    plan: ["retryable_after_commit", "retryable_after_commit", "retryable_after_commit"],
  },
};

type Errors = { order?: string; threshold?: string; submit?: string };

export function CreateRunForm({ onCreated }: { onCreated: (run: Run) => void }) {
  const [orderId, setOrderId] = useState("ORD-1001");
  const [thresholdRupees, setThresholdRupees] = useState("5000");
  const [manualReview, setManualReview] = useState(false);
  const [faults, setFaults] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Errors>({});
  const [submitting, setSubmitting] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const next: Errors = {};
    if (!orderId.trim()) next.order = "Enter an order id.";
    if (!/^\d+$/.test(thresholdRupees.trim())) next.threshold = "Enter a whole number of rupees, 0 or more.";
    setErrors(next);
    if (next.order || next.threshold) return;

    const plan: Record<string, FaultKind[]> = {};
    for (const [step, preset] of Object.entries(faults)) {
      if (preset !== "none") plan[step] = FAULT_PRESETS[preset].plan;
    }
    setSubmitting(true);
    try {
      const run = await api.createRun(orderId.trim(), {
        approval_threshold_paise: Number(thresholdRupees.trim()) * 100,
        require_manual_review: manualReview,
        faults: plan,
      });
      onCreated(run);
    } catch (err) {
      setErrors({ submit: messageOf(err) });
    } finally {
      setSubmitting(false);
    }
  }

  const activeFaults = Object.values(faults).filter((p) => p !== "none").length;

  return (
    <form className="panel" onSubmit={submit} noValidate aria-labelledby="create-title">
      <h2 id="create-title">New refund run</h2>

      <label htmlFor="order-id">Order id</label>
      <input
        id="order-id"
        list="demo-orders"
        value={orderId}
        onChange={(e) => setOrderId(e.target.value)}
        aria-invalid={!!errors.order}
        aria-describedby={errors.order ? "order-error" : undefined}
        autoComplete="off"
        spellCheck={false}
      />
      <datalist id="demo-orders">
        {DEMO_ORDERS.map(([id, note]) => (
          <option key={id} value={id}>
            {note}
          </option>
        ))}
      </datalist>
      {errors.order && <p id="order-error" className="field-error">{errors.order}</p>}

      <label htmlFor="threshold">Approval threshold (₹)</label>
      <input
        id="threshold"
        inputMode="numeric"
        value={thresholdRupees}
        onChange={(e) => setThresholdRupees(e.target.value)}
        aria-invalid={!!errors.threshold}
        aria-describedby={errors.threshold ? "threshold-error" : "threshold-hint"}
      />
      {errors.threshold ? (
        <p id="threshold-error" className="field-error">{errors.threshold}</p>
      ) : (
        <p id="threshold-hint" className="hint">Refunds strictly above this need approval.</p>
      )}

      <label className="checkbox">
        <input type="checkbox" checked={manualReview} onChange={(e) => setManualReview(e.target.checked)} />
        Require manual review
      </label>

      <details className="faults">
        <summary>
          Fault injection{activeFaults > 0 && <span className="count"> · {activeFaults} active</span>}
        </summary>
        <p className="hint">Deterministic mock failures per step, saved with the run.</p>
        {PIPELINE.map((step) => (
          <div className="fault-row" key={step}>
            <label htmlFor={`fault-${step}`} className="mono">{step}</label>
            <select
              id={`fault-${step}`}
              value={faults[step] ?? "none"}
              onChange={(e) => setFaults({ ...faults, [step]: e.target.value })}
            >
              {Object.entries(FAULT_PRESETS).map(([key, { label }]) => (
                <option key={key} value={key}>{label}</option>
              ))}
            </select>
          </div>
        ))}
      </details>

      {errors.submit && <p className="alert alert-error" role="alert">{errors.submit}</p>}
      <button type="submit" className="primary" disabled={submitting}>
        {submitting ? "Running…" : "Start run"}
      </button>
    </form>
  );
}
