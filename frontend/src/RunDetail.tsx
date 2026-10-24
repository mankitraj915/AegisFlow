import { useEffect, useState, type FormEvent } from "react";
import { api, messageOf, type Approval, type RunDetail as Detail, type RunEvent, type Step } from "./api";
import { formatPaise, formatTime, outcomeUnknownEvent, StatusBadge } from "./format";

type Loaded = { run: Detail; events: RunEvent[] };

export function RunDetail({ runId, onChanged }: { runId: string; onChanged: () => void }) {
  const [data, setData] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const reload = () => setVersion((v) => v + 1);

  useEffect(() => {
    let current = true; // Ignore responses for a run that is no longer selected.
    setError(null);
    Promise.all([api.getRun(runId), api.listEvents(runId)]).then(
      ([run, events]) => current && setData({ run, events }),
      (err) => current && setError(messageOf(err)),
    );
    return () => {
      current = false;
    };
  }, [runId, version]);

  if (error) {
    return (
      <section className="panel">
        <p className="alert alert-error" role="alert">{error}</p>
        <button type="button" onClick={reload}>Retry</button>
      </section>
    );
  }
  if (!data || data.run.id !== runId) {
    return <section className="panel"><p className="muted" aria-live="polite">Loading run…</p></section>;
  }

  const { run, events } = data;
  const unknown = outcomeUnknownEvent(events);
  const { scenario } = run;
  const faultSteps = Object.keys(scenario.faults ?? {});

  return (
    <section className="detail" aria-labelledby="detail-title">
      <header className="panel detail-head">
        <div className="detail-title-row">
          <h2 id="detail-title"><span className="mono">{run.order_id}</span></h2>
          <StatusBadge status={unknown ? "outcome_unknown" : run.interrupted ? "interrupted" : run.status} />
          <button type="button" className="push-right" onClick={reload}>Reload</button>
        </div>
        <dl className="facts">
          <div><dt>Run</dt><dd className="mono">{run.id}</dd></div>
          <div><dt>Created</dt><dd>{formatTime(run.created_at)}</dd></div>
          <div><dt>Threshold</dt><dd>{formatPaise(scenario.approval_threshold_paise)}</dd></div>
          <div><dt>Manual review</dt><dd>{scenario.require_manual_review ? "Required" : "Not required"}</dd></div>
          {faultSteps.length > 0 && (
            <div><dt>Injected faults</dt><dd className="mono">{faultSteps.map((s) => `${s}: ${scenario.faults[s].join(", ")}`).join(" · ")}</dd></div>
          )}
        </dl>

        {unknown && (
          <div className="alert alert-unknown" role="status">
            <strong>Refund outcome unknown.</strong> The refund step ran out of retries after the ledger
            write may have committed. Check the ledger for idempotency key{" "}
            <code>{String(unknown.payload.idempotency_key)}</code> before taking further action.
            <p className="alert-detail">{run.error}</p>
          </div>
        )}
        {!unknown && run.status === "failed" && run.error && (
          <p className="alert alert-error" role="status"><strong>Failed:</strong> {run.error}</p>
        )}
      </header>

      {run.status === "running" && (
        <ResumePanel
          runId={run.id}
          interrupted={run.interrupted}
          step={run.steps.find((s) => s.status === "started")}
          onResumed={() => { reload(); onChanged(); }}
        />
      )}

      {run.status === "awaiting_approval" && run.pending_approval && (
        <ApprovalPanel runId={run.id} approval={run.pending_approval} onDecided={() => { reload(); onChanged(); }} />
      )}

      <section className="panel" aria-labelledby="steps-title">
        <h3 id="steps-title">Steps</h3>
        {run.steps.length === 0 ? (
          <p className="muted">No steps recorded.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>#</th><th>Step</th><th>Status</th><th>Attempts</th><th>Idempotency key</th><th>Last error</th></tr>
              </thead>
              <tbody>
                {run.steps.map((s) => (
                  <tr key={s.idx}>
                    <td>{s.idx}</td>
                    <td className="mono">{s.name}</td>
                    <td><StatusBadge status={s.status} /></td>
                    <td className={s.attempt > 1 ? "retried" : undefined}>{s.attempt}</td>
                    <td className="mono small">{s.idempotency_key}</td>
                    <td className="small">{s.error ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel" aria-labelledby="ledger-title">
        <h3 id="ledger-title">Refund ledger</h3>
        <p className="hint">Rows in the simulated payment system written under this run's idempotency keys.</p>
        {run.refunds.length === 0 ? (
          <p className="muted">No refund recorded.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Idempotency key</th><th>Order</th><th>Amount</th><th>Recorded</th></tr>
              </thead>
              <tbody>
                {run.refunds.map((r) => (
                  <tr key={r.idempotency_key}>
                    <td className="mono small">{r.idempotency_key}</td>
                    <td className="mono">{r.order_id}</td>
                    <td>{formatPaise(r.amount_paise)}</td>
                    <td className="small">{formatTime(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel" aria-labelledby="events-title">
        <h3 id="events-title">Event history</h3>
        <ol className="events">
          {events.map((e) => (
            <li key={e.seq} className={`event event-${tone(e)}`}>
              <span className="event-seq">{e.seq}</span>
              <div className="event-body">
                <div className="event-line">
                  <span className="mono event-type">{e.type}</span>
                  <time className="small muted" dateTime={e.ts}>{formatTime(e.ts)}</time>
                </div>
                {summarize(e) && <p className="event-summary">{summarize(e)}</p>}
                <details>
                  <summary className="small">Payload</summary>
                  <pre>{JSON.stringify(e.payload, null, 2)}</pre>
                </details>
              </div>
            </li>
          ))}
        </ol>
      </section>
    </section>
  );
}

function ResumePanel({ runId, interrupted, step, onResumed }: {
  runId: string;
  interrupted: boolean;
  step: Step | undefined;
  onResumed: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function resume() {
    setError(null);
    setPending(true);
    try {
      await api.resume(runId);
      onResumed();
    } catch (err) {
      // A 409 means the run is already being advanced or is no longer running; Reload shows which.
      setError(messageOf(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="panel approval" aria-labelledby="resume-title">
      <h3 id="resume-title">{interrupted ? "Interrupted (simulated)" : "Run not finished"}</h3>
      <p>
        {interrupted
          ? "An injected fault stopped this run at a known point: after the step's tool call committed, before the step was recorded as succeeded. The server process did not restart."
          : "This run stopped before reaching a terminal state, or is still being advanced by another request."}
      </p>
      {step && (
        <p>
          Step <span className="mono">#{step.idx} {step.name}</span> is in progress with idempotency key{" "}
          <code>{step.idempotency_key}</code>. Resuming re-runs it with the same key, so a refund already in
          the ledger is replayed rather than written twice.
        </p>
      )}
      {error && <p className="alert alert-error" role="alert">{error}</p>}
      <div className="actions">
        <button type="button" className="primary" disabled={pending} onClick={resume}>
          {pending ? "Resuming…" : "Resume run"}
        </button>
      </div>
    </section>
  );
}

function ApprovalPanel({ runId, approval, onDecided }: { runId: string; approval: Approval; onDecided: () => void }) {
  const [decidedBy, setDecidedBy] = useState("");
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nameError, setNameError] = useState(false);

  async function decide(e: FormEvent, decision: "approve" | "reject") {
    e.preventDefault();
    if (!decidedBy.trim()) {
      setNameError(true);
      return;
    }
    setNameError(false);
    setError(null);
    setPending(decision);
    try {
      if (decision === "approve") await api.approve(runId, decidedBy.trim());
      else await api.reject(runId, decidedBy.trim(), reason.trim() || null);
      onDecided();
    } catch (err) {
      // A 409 means a decision was already recorded; Reload shows the current state.
      setError(messageOf(err));
    } finally {
      setPending(null);
    }
  }

  const { requested_payload: req } = approval;
  return (
    <form className="panel approval" onSubmit={(e) => decide(e, "approve")} noValidate aria-labelledby="approval-title">
      <h3 id="approval-title">Approval required</h3>
      <p>
        <span className="mono">{req.step}</span> will refund <strong>{formatPaise(req.amount_paise)}</strong>.
        Reason: {req.reason}.
      </p>
      <div className="approval-fields">
        <div>
          <label htmlFor="decided-by">Your name or email</label>
          <input
            id="decided-by"
            value={decidedBy}
            onChange={(e) => setDecidedBy(e.target.value)}
            aria-invalid={nameError}
            aria-describedby={nameError ? "decided-by-error" : undefined}
            autoComplete="email"
          />
          {nameError && <p id="decided-by-error" className="field-error">Required to record who decided.</p>}
        </div>
        <div>
          <label htmlFor="reject-reason">Rejection reason (optional)</label>
          <input id="reject-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
      </div>
      {error && <p className="alert alert-error" role="alert">{error}</p>}
      <div className="actions">
        <button type="submit" className="primary" disabled={pending !== null}>
          {pending === "approve" ? "Approving…" : "Approve and issue refund"}
        </button>
        <button type="button" className="danger" disabled={pending !== null} onClick={(e) => decide(e, "reject")}>
          {pending === "reject" ? "Rejecting…" : "Reject"}
        </button>
      </div>
    </form>
  );
}

function tone(e: RunEvent): string {
  if (e.type === "run_failed" || e.type === "step_failed" || e.type === "run_rejected") return "bad";
  if (e.type === "step_attempt_failed" || e.type === "approval_requested" || e.type === "run_interrupted") return "warn";
  if (e.type === "run_succeeded") return "good";
  return "plain";
}

function summarize(e: RunEvent): string {
  const p = e.payload;
  const step = `#${p.idx} ${p.name}`;
  const attempt = p.attempt === undefined ? "" : ` · attempt ${p.attempt}`;
  switch (e.type) {
    case "run_created": return `Order ${p.order_id}`;
    case "step_started": return step;
    case "step_succeeded": return `${step}${attempt}`;
    case "step_attempt_failed": return `${step}${attempt} failed, retrying in ${p.next_delay_s}s: ${p.error}`;
    case "step_failed": return `${step}${attempt}: ${p.error}`;
    case "run_failed": return String(p.error);
    case "run_interrupted": return `Simulated interruption after ${step}${attempt}, key ${p.idempotency_key}`;
    case "run_resumed": return p.name === undefined ? "Resumed" : `Resuming ${step} with key ${p.idempotency_key}`;
    case "approval_requested": return `${p.step}: ${p.reason}`;
    case "approval_approved": return `Approved by ${p.decided_by}`;
    case "approval_rejected": return `Rejected by ${p.decided_by}${p.reason ? `: ${p.reason}` : ""}`;
    default: return "";
  }
}
