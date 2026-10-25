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
  interrupt: { label: "Simulated interruption after commit (resume manually)", plan: ["interrupt_after_commit"] },
};

interface Config {
  orderId: string;
  thresholdRupees: string;
  manualReview: boolean;
  faults: Record<string, string>; // step -> FAULT_PRESETS key
}

// Each scenario is only a form configuration of behavior the backend supports.
const SCENARIOS: { key: string; title: string; what: string; config: Config }[] = [
  {
    key: "complete",
    title: "Clean completion",
    what: "ORD-1001, ₹1,299. All six steps succeed without approval.",
    config: { orderId: "ORD-1001", thresholdRupees: "5000", manualReview: false, faults: {} },
  },
  {
    key: "approval",
    title: "Approval required",
    what: "ORD-2002, ₹12,000 is over the ₹5,000 threshold, so the run pauses before the refund.",
    config: { orderId: "ORD-2002", thresholdRupees: "5000", manualReview: false, faults: {} },
  },
  {
    key: "retry",
    title: "Retry after commit",
    what: "The refund commits, then times out once. The retry reuses the key: one ledger entry.",
    config: { orderId: "ORD-1001", thresholdRupees: "5000", manualReview: false, faults: { issue_refund: "commit_once" } },
  },
  {
    key: "unknown",
    title: "Outcome unknown",
    what: "The refund times out after commit on all 3 attempts. The run fails with its outcome unknown.",
    config: { orderId: "ORD-1001", thresholdRupees: "5000", manualReview: false, faults: { issue_refund: "commit_exhaust" } },
  },
  {
    key: "interrupt",
    title: "Interruption and resume",
    what: "The run stops after the ledger write, before the step is recorded. Resume it to finish.",
    config: { orderId: "ORD-1001", thresholdRupees: "5000", manualReview: false, faults: { issue_refund: "interrupt" } },
  },
  {
    key: "policy",
    title: "Policy failure",
    what: "ORD-9009 was cancelled, so the policy check fails the run before any refund.",
    config: { orderId: "ORD-9009", thresholdRupees: "5000", manualReview: false, faults: {} },
  },
];

type Errors = { order?: string; threshold?: string; submit?: string };

export function CreateRunForm({ onCreated }: { onCreated: (run: Run) => void }) {
  const [scenario, setScenario] = useState<string | null>("complete");
  const [config, setConfig] = useState<Config>(SCENARIOS[0].config);
  const [errors, setErrors] = useState<Errors>({});
  const [submitting, setSubmitting] = useState(false);
  const { orderId, thresholdRupees, manualReview, faults } = config;

  // Any manual edit turns the selection into a custom configuration.
  const edit = (patch: Partial<Config>) => {
    setConfig({ ...config, ...patch });
    setScenario(null);
  };

  function choose(key: string) {
    setConfig(SCENARIOS.find((s) => s.key === key)!.config);
    setScenario(key);
    setErrors({});
  }

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

  const activeFaults = Object.entries(faults).filter(([, p]) => p !== "none");

  return (
    <form className="composer" onSubmit={submit} noValidate aria-labelledby="create-title">
      <h2 id="create-title" className="section-title">Start a run</h2>

      <fieldset className="scenarios">
        <legend className="label">Scenario</legend>
        {SCENARIOS.map((s) => (
          <label key={s.key} className="scenario">
            <input
              type="radio"
              name="scenario"
              value={s.key}
              checked={scenario === s.key}
              onChange={() => choose(s.key)}
            />
            <span className="scenario-text">
              <span className="scenario-title">{s.title}</span>
              <span className="scenario-what">{s.what}</span>
            </span>
          </label>
        ))}
        {scenario === null && <p className="hint custom-note">Custom configuration: you have edited the fields below.</p>}
      </fieldset>

      <div className="fields">
        <div className="field">
          <label htmlFor="order-id">Order id</label>
          <input
            id="order-id"
            className="mono"
            list="demo-orders"
            value={orderId}
            onChange={(e) => edit({ orderId: e.target.value })}
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
        </div>

        <div className="field">
          <label htmlFor="threshold">Approval threshold (₹)</label>
          <input
            id="threshold"
            inputMode="numeric"
            value={thresholdRupees}
            onChange={(e) => edit({ thresholdRupees: e.target.value })}
            aria-invalid={!!errors.threshold}
            aria-describedby={errors.threshold ? "threshold-error" : "threshold-hint"}
          />
          {errors.threshold ? (
            <p id="threshold-error" className="field-error">{errors.threshold}</p>
          ) : (
            <p id="threshold-hint" className="hint">Refunds strictly above this need approval.</p>
          )}
        </div>
      </div>

      <label className="checkbox">
        <input type="checkbox" checked={manualReview} onChange={(e) => edit({ manualReview: e.target.checked })} />
        Require manual review
      </label>

      <details className="faults">
        <summary>
          Per-step fault injection
          {activeFaults.length > 0 && <span className="fault-count"> · {activeFaults.length} active</span>}
        </summary>
        <p className="hint">Deterministic outcomes for each attempt of a step, saved with the run.</p>
        {PIPELINE.map((step) => (
          <div className="fault-row" key={step}>
            <label htmlFor={`fault-${step}`} className="mono">{step}</label>
            <select
              id={`fault-${step}`}
              value={faults[step] ?? "none"}
              onChange={(e) => edit({ faults: { ...faults, [step]: e.target.value } })}
            >
              {Object.entries(FAULT_PRESETS).map(([key, { label }]) => (
                <option key={key} value={key}>{label}</option>
              ))}
            </select>
          </div>
        ))}
      </details>

      {activeFaults.length > 0 && (
        <p className="hint fault-summary">
          Faults: {activeFaults.map(([step, p]) => `${step}: ${FAULT_PRESETS[p].label.toLowerCase()}`).join("; ")}
        </p>
      )}
      {errors.submit && <p className="alert tone-fail" role="alert">{errors.submit}</p>}
      <button type="submit" className="primary" disabled={submitting}>
        {submitting ? "Running…" : "Start run"}
      </button>
      <p className="hint">The run executes as soon as you start it.</p>
    </form>
  );
}
