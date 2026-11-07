import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, messageOf, PIPELINE, type FaultKind, type OrderKind, type Run } from "./api";

// Mock orders are seeded in backend/aegisflow/demo_data.py, and each can be refunded
// only up to its total, so the suggestions come from the server: the next order of
// each kind with something left. Any id can still be typed; unknown ones get a 404.
const FIRST_ORDER: Record<OrderKind, string> = {
  under_threshold: "ORD-1001",
  over_threshold: "ORD-2002",
  manual_review: "ORD-3003",
  at_threshold: "ORD-4004",
  cancelled: "ORD-9009",
};
const ORDER_NOTES: Record<OrderKind, string> = {
  under_threshold: "₹1,299 · under default threshold",
  over_threshold: "₹12,000 · over default threshold",
  manual_review: "₹499 · flagged for manual review",
  at_threshold: "₹5,000 · exactly at default threshold",
  cancelled: "cancelled · not refundable",
};

// Per-attempt fault plans. The engine retries up to 3 attempts in total.
const FAULT_PRESETS: Record<string, { label: string; plan: FaultKind[] }> = {
  none: { label: "No fault", plan: [] },
  transient: { label: "Fails once, then succeeds", plan: ["retryable"] },
  exhaust: { label: "Fails on every attempt", plan: ["retryable", "retryable", "retryable"] },
  fatal: { label: "Fatal error", plan: ["fatal"] },
  commit_once: { label: "Times out after commit, once", plan: ["retryable_after_commit"] },
  interrupt: { label: "Simulated interruption after commit (resume manually)", plan: ["interrupt_after_commit"] },
};

interface Config {
  orderId: string;
  thresholdRupees: string;
  manualReview: boolean;
  faults: Record<string, string>; // step -> FAULT_PRESETS key
  statusDown?: boolean;
}

// Each scenario is only a form configuration of behavior the backend supports.
const SCENARIOS: { key: string; title: string; what: string; kind: OrderKind; config: Omit<Config, "orderId"> }[] = [
  {
    key: "complete",
    title: "Clean completion",
    what: "A ₹1,299 order. All six steps succeed without approval.",
    kind: "under_threshold",
    config: { thresholdRupees: "5000", manualReview: false, faults: {} },
  },
  {
    key: "approval",
    title: "Approval required",
    what: "A ₹12,000 order is over the ₹5,000 threshold, so the run pauses before the refund.",
    kind: "over_threshold",
    config: { thresholdRupees: "5000", manualReview: false, faults: {} },
  },
  {
    key: "retry",
    title: "Retry after commit",
    what: "The refund commits, then times out. The engine asks the provider, finds it, and calls no further: one ledger entry.",
    kind: "under_threshold",
    config: { thresholdRupees: "5000", manualReview: false, faults: { issue_refund: "commit_once" } },
  },
  {
    key: "unknown",
    title: "Outcome unknown",
    what: "The refund times out after commit and the status lookup is down too, so nobody can tell. The run is held for review, not failed.",
    kind: "under_threshold",
    config: { thresholdRupees: "5000", manualReview: false, faults: { issue_refund: "commit_once" }, statusDown: true },
  },
  {
    key: "interrupt",
    title: "Interruption and resume",
    what: "The run stops after the ledger write, before the step is recorded. Resume it to finish.",
    kind: "under_threshold",
    config: { thresholdRupees: "5000", manualReview: false, faults: { issue_refund: "interrupt" } },
  },
  {
    key: "policy",
    title: "Policy failure",
    what: "ORD-9009 was cancelled, so the policy check fails the run before any refund.",
    kind: "cancelled",
    config: { thresholdRupees: "5000", manualReview: false, faults: {} },
  },
];

type Errors = { order?: string; threshold?: string; submit?: string };

export function CreateRunForm({ onCreated }: { onCreated: (run: Run) => void }) {
  const [scenario, setScenario] = useState<string | null>("complete");
  const [config, setConfig] = useState<Config>({ ...SCENARIOS[0].config, orderId: FIRST_ORDER[SCENARIOS[0].kind] });
  const [available, setAvailable] = useState<Partial<Record<OrderKind, string>>>({});
  // Kept across a failed submit, so retrying after a lost response can't create a second run.
  const requestKey = useRef(crypto.randomUUID());
  const [errors, setErrors] = useState<Errors>({});
  const [submitting, setSubmitting] = useState(false);
  const { orderId, thresholdRupees, manualReview, faults, statusDown } = config;

  // Picks the next usable order for the selected scenario. A failure only leaves the defaults in place.
  async function refreshOrders(selected: string | null) {
    try {
      const next = Object.fromEntries((await api.availableOrders()).map((o) => [o.kind, o.id]));
      setAvailable(next);
      const kind = SCENARIOS.find((s) => s.key === selected)?.kind;
      if (kind && next[kind]) setConfig((c) => ({ ...c, orderId: next[kind] }));
    } catch {
      setAvailable({});
    }
  }

  useEffect(() => {
    void refreshOrders("complete");
  }, []);

  // Any manual edit turns the selection into a custom configuration, and a new request.
  const edit = (patch: Partial<Config>) => {
    setConfig({ ...config, ...patch });
    setScenario(null);
    requestKey.current = crypto.randomUUID();
  };

  function choose(key: string) {
    const chosen = SCENARIOS.find((s) => s.key === key)!;
    setConfig({ ...chosen.config, orderId: available[chosen.kind] ?? FIRST_ORDER[chosen.kind] });
    setScenario(key);
    setErrors({});
    requestKey.current = crypto.randomUUID();
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
        provider_status_down: !!statusDown,
      }, requestKey.current);
      requestKey.current = crypto.randomUUID();
      onCreated(run);
      void refreshOrders(scenario);
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
            {(Object.keys(ORDER_NOTES) as OrderKind[]).map((kind) => (
              <option key={kind} value={available[kind] ?? FIRST_ORDER[kind]}>
                {ORDER_NOTES[kind]}
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
        <label className="checkbox">
          <input type="checkbox" checked={!!statusDown} onChange={(e) => edit({ statusDown: e.target.checked })} />
          Provider status lookup is down
        </label>
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
