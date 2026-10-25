import { useEffect, useState, type FormEvent } from "react";
import { api, messageOf, PIPELINE, type Approval, type RunDetail as Detail, type RunEvent, type Step } from "./api";
import {
  displayStatus, formatPaise, formatTime, Ident, outcomeUnknownEvent, shortId, STEP_INFO, StatusMark, type Tone,
} from "./format";

type Loaded = { run: Detail; events: RunEvent[] };

// Where each pipeline step stands, derived from recorded steps, events and the run status.
type StationState = "done" | "active" | "interrupted" | "failed" | "unknown" | "gate" | "rejected" | "pending" | "skipped";

const STATION_LABEL: Record<StationState, string> = {
  done: "Succeeded",
  active: "In progress",
  interrupted: "Interrupted",
  failed: "Failed",
  unknown: "Outcome unknown",
  gate: "Awaiting approval",
  rejected: "Not run · rejected",
  pending: "Pending",
  skipped: "Not run",
};

interface Station {
  idx: number;
  name: string;
  state: StationState;
  step: Step | undefined;
  failedAttempts: Set<number>;
}

function stationsOf(run: Detail, events: RunEvent[], unknown: boolean): Station[] {
  const stopped = run.status === "failed" || run.status === "rejected";
  return PIPELINE.map((name, idx) => {
    const step = run.steps[idx];
    let state: StationState;
    if (step) {
      if (step.status === "succeeded") state = "done";
      else if (step.status === "failed") state = unknown ? "unknown" : "failed";
      else state = run.interrupted ? "interrupted" : "active";
    } else if (idx === run.steps.length && run.status === "awaiting_approval") state = "gate";
    else if (idx === run.steps.length && run.status === "rejected") state = "rejected";
    else state = stopped ? "skipped" : "pending";
    const failedAttempts = new Set(
      events.filter((e) => e.type === "step_attempt_failed" && e.payload.idx === idx).map((e) => Number(e.payload.attempt)),
    );
    return { idx, name, state, step, failedAttempts };
  });
}

// The step an event belongs to, if any.
const eventIdx = (e: RunEvent): number | undefined =>
  typeof e.payload.idx === "number" ? e.payload.idx : typeof e.payload.step_idx === "number" ? e.payload.step_idx : undefined;

export function RunDetail({ runId, onChanged }: { runId: string; onChanged: () => void }) {
  const [data, setData] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [focus, setFocus] = useState<number | null>(null);
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

  useEffect(() => setFocus(null), [runId]);

  if (error) {
    return (
      <section className="run-error">
        <p className="alert tone-fail" role="alert">{error}</p>
        <button type="button" onClick={reload}>Retry</button>
      </section>
    );
  }
  if (!data || data.run.id !== runId) {
    return <p className="muted" aria-live="polite">Loading run…</p>;
  }

  const { run, events } = data;
  const unknown = outcomeUnknownEvent(events);
  const stations = stationsOf(run, events, !!unknown);
  const { scenario } = run;
  const faultSteps = Object.keys(scenario.faults ?? {});
  const proposed = run.steps[3]?.output?.amount_paise;
  const changed = () => { reload(); onChanged(); };

  const started = run.steps.find((s) => s.status === "started");
  const actionAt = run.status === "awaiting_approval" && run.pending_approval
    ? run.pending_approval.step_idx
    : run.status === "running" ? (started?.idx ?? run.steps.length) : null;

  return (
    <article className="detail" aria-labelledby="detail-title">
      <header className="detail-head">
        <div className="detail-title-row">
          <p className="label">Run <span className="mono">{shortId(run.id)}</span></p>
          <button type="button" className="quiet" onClick={reload}>Reload</button>
        </div>
        <h2 id="detail-title" className="view-title">
          <span className="mono">{run.order_id}</span>
          <StatusMark status={displayStatus(run, !!unknown)} />
        </h2>
        <Outcome run={run} events={events} unknownEvent={unknown} />
        <dl className="facts">
          <div><dt>Run id</dt><dd className="mono">{run.id}</dd></div>
          <div><dt>Created</dt><dd>{formatTime(run.created_at)}</dd></div>
          {typeof proposed === "number" && <div><dt>Refund</dt><dd className="num">{formatPaise(proposed)}</dd></div>}
          <div><dt>Threshold</dt><dd className="num">{formatPaise(scenario.approval_threshold_paise)}</dd></div>
          <div><dt>Manual review</dt><dd>{scenario.require_manual_review ? "Required" : "Not required"}</dd></div>
          <div>
            <dt>Injected faults</dt>
            <dd className={faultSteps.length ? "mono" : undefined}>
              {faultSteps.length
                ? <Ident>{faultSteps.map((s) => `${s}: ${scenario.faults[s].join(", ")}`).join(" · ")}</Ident>
                : "None"}
            </dd>
          </div>
        </dl>
      </header>

      <section className="trace" aria-labelledby="trace-title">
        <div className="section-head">
          <h3 id="trace-title" className="section-title">Execution trace</h3>
          <p className="hint">Select a step to filter its evidence.</p>
        </div>
        <ol className="rail">
          {stations.map((st) => (
            <StationItem
              key={st.name}
              station={st}
              runId={run.id}
              approval={st.name === "issue_refund" ? approvalNote(events) : null}
              focused={focus === st.idx}
              onFocus={() => setFocus(focus === st.idx ? null : st.idx)}
            />
          ))}
        </ol>
        {actionAt !== null && (
          <div className={`callout tone-${run.status === "awaiting_approval" ? "gate" : run.interrupted ? "cut" : "run"}`}
            style={{ "--at": actionAt } as React.CSSProperties}>
            {run.status === "awaiting_approval" && run.pending_approval ? (
              <ApprovalPanel runId={run.id} approval={run.pending_approval} onDecided={changed} />
            ) : (
              <ResumePanel interrupted={run.interrupted} step={started} runId={run.id} onResumed={changed} />
            )}
          </div>
        )}
      </section>

      <div className="evidence">
        <Ledger run={run} focus={focus} />
        <EventLog events={events} focus={focus} onClear={() => setFocus(null)} />
      </div>
    </article>
  );
}

function StationItem({ station, runId, approval, focused, onFocus }: {
  station: Station;
  runId: string;
  approval: string | null;
  focused: boolean;
  onFocus: () => void;
}) {
  const { idx, name, state, step, failedAttempts } = station;
  const attempts = step?.attempt ?? 0;
  return (
    <li className={`station state-${state}`}>
      <button type="button" className="station-body" aria-pressed={focused} onClick={onFocus}>
        <span className="node" aria-hidden="true" />
        <span className="station-index">{idx}</span>
        <span className="station-title">{STEP_INFO[name].title}</span>
        <span className="station-id mono"><Ident>{name}</Ident></span>
        <span className="station-state">{STATION_LABEL[state]}</span>
        {attempts > 0 && (
          <span className="attempts">
            <span className="ticks" aria-hidden="true">
              {Array.from({ length: attempts }, (_, i) => (
                <span key={i} className={`tick ${failedAttempts.has(i + 1) ? "tick-failed" : "tick-final"}`} />
              ))}
            </span>
            {attempts === 1 ? "1 attempt" : `${attempts} attempts`}
            {failedAttempts.size > 0 && `, ${failedAttempts.size} failed`}
          </span>
        )}
        {step && (
          <span className="station-key mono" title={step.idempotency_key}>
            key {shortId(runId)}…:{idx}
          </span>
        )}
        {approval && <span className="station-gate">{approval}</span>}
        {step?.error && state !== "done" && <span className="station-error">{step.error}</span>}
      </button>
    </li>
  );
}

// The approval gate in front of issue_refund, as recorded in events.
function approvalNote(events: RunEvent[]): string | null {
  const decided = events.find((e) => e.type === "approval_approved" || e.type === "approval_rejected");
  if (decided) return `${decided.type === "approval_approved" ? "Approved" : "Rejected"} by ${decided.payload.decided_by}`;
  return events.some((e) => e.type === "approval_requested") ? "Approval requested" : null;
}

function Outcome({ run, events, unknownEvent }: { run: Detail; events: RunEvent[]; unknownEvent: RunEvent | undefined }) {
  let tone: Tone = "idle";
  let text: React.ReactNode = null;
  const keyed = (k: string) => <code>{k}</code>;
  if (unknownEvent) {
    tone = "unknown";
    text = (
      <>
        The refund step ran out of retries after the ledger write may have committed. Check the ledger for key{" "}
        {keyed(String(unknownEvent.payload.idempotency_key))} before taking further action.
        <span className="outcome-detail">{run.error}</span>
      </>
    );
  } else if (run.status === "succeeded") {
    tone = "ok";
    const [refund, ...more] = run.refunds;
    text = refund && more.length === 0
      ? <>Refund of {formatPaise(refund.amount_paise)} recorded once, under key {keyed(refund.idempotency_key)}. Customer email composed.</>
      : <>All steps succeeded. The ledger holds {run.refunds.length} entries for this run.</>;
  } else if (run.status === "failed") {
    tone = "fail";
    text = <>{run.error}</>;
  } else if (run.status === "rejected") {
    tone = "fail";
    const rejected = events.find((e) => e.type === "approval_rejected");
    text = (
      <>
        Rejected by {String(rejected?.payload.decided_by)}
        {rejected?.payload.reason ? `: ${rejected.payload.reason}` : ""}.{" "}
        {run.refunds.length === 0 ? "No refund was issued." : ""}
      </>
    );
  } else if (run.status === "awaiting_approval" && run.pending_approval) {
    tone = "gate";
    const req = run.pending_approval.requested_payload;
    text = <>Paused before step {run.pending_approval.step_idx}, <span className="mono">{req.step}</span>: {req.reason}.</>;
  } else if (run.interrupted) {
    tone = "cut";
    text = <>A simulated interruption stopped the run after the tool call and before the step was recorded. Resume it to finish with the same key.</>;
  } else if (run.status === "running") {
    tone = "run";
    text = <>This run has not reached a terminal state. It stopped mid-run, or another request is still advancing it.</>;
  }
  return text && <p className={`outcome tone-${tone}`} role="status">{text}</p>;
}

function Ledger({ run, focus }: { run: Detail; focus: number | null }) {
  const writtenBy = (key: string) => run.steps.find((s) => s.idempotency_key === key);
  return (
    <section className="ledger" aria-labelledby="ledger-title">
      <div className="section-head">
        <h3 id="ledger-title" className="section-title">
          Ledger{run.refunds.length > 0 && <span className="count"> {run.refunds.length}</span>}
        </h3>
      </div>
      <p className="hint">The simulated payment system's rows under this run's idempotency keys.</p>
      {run.refunds.length === 0 ? (
        <p className="empty">No refund recorded.</p>
      ) : (
        <ul className="entries">
          {run.refunds.map((r) => {
            const step = writtenBy(r.idempotency_key);
            return (
              <li key={r.idempotency_key} className={focus !== null && step?.idx !== focus ? "dim" : undefined}>
                <span className="entry-amount num">{formatPaise(r.amount_paise)}</span>
                <span className="entry-key mono">{r.idempotency_key}</span>
                <span className="entry-meta">
                  <span className="mono">{r.order_id}</span> · {formatTime(r.created_at)}
                  {step && <> · step {step.idx} <span className="mono">{step.name}</span> ({step.status === "started" ? "not yet recorded" : step.status})</>}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function EventLog({ events, focus, onClear }: { events: RunEvent[]; focus: number | null; onClear: () => void }) {
  const shown = focus === null ? events : events.filter((e) => eventIdx(e) === focus);
  return (
    <section className="events" aria-labelledby="events-title">
      <div className="section-head">
        <h3 id="events-title" className="section-title">
          Event log <span className="count">{focus === null ? events.length : `${shown.length} of ${events.length}`}</span>
        </h3>
        {focus !== null && (
          <button type="button" className="quiet" onClick={onClear}>Show all events</button>
        )}
      </div>
      <p className="hint">
        {focus === null ? "Every transition the engine recorded, in order." : `Events for step ${focus}, ${PIPELINE[focus]}.`}
      </p>
      <ol className="event-list">
        {shown.map((e) => (
          <li key={e.seq} className={`event tone-${tone(e)}`}>
            <span className="event-seq num">{e.seq}</span>
            <div className="event-body">
              <div className="event-line">
                <span className="mono event-type">{e.type}</span>
                <time className="event-time" dateTime={e.ts}>{formatTime(e.ts)}</time>
              </div>
              {summarize(e) && <p className="event-summary">{summarize(e)}</p>}
              <details>
                <summary>Payload</summary>
                <pre>{JSON.stringify(e.payload, null, 2)}</pre>
              </details>
            </div>
          </li>
        ))}
      </ol>
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
    <section aria-labelledby="resume-title">
      <h4 id="resume-title" className="callout-title">{interrupted ? "Interrupted (simulated)" : "Run not finished"}</h4>
      <p>
        {interrupted
          ? "An injected fault stopped this run at a known point: after the step's tool call committed, before the step was recorded as succeeded. The server process did not restart."
          : "This run stopped before reaching a terminal state, or is still being advanced by another request."}
      </p>
      {step && (
        <p>
          Step {step.idx}, <span className="mono">{step.name}</span>, is in progress with idempotency key{" "}
          <code>{step.idempotency_key}</code>. Resuming re-runs it with the same key, so a refund already in the
          ledger is replayed rather than written twice.
        </p>
      )}
      {error && <p className="alert tone-fail" role="alert">{error}</p>}
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
    <form onSubmit={(e) => decide(e, "approve")} noValidate aria-labelledby="approval-title">
      <h4 id="approval-title" className="callout-title">Approval required</h4>
      <p>
        <span className="mono">{req.step}</span> will refund <strong className="num">{formatPaise(req.amount_paise)}</strong>.
        Reason: {req.reason}.
      </p>
      <div className="approval-fields">
        <div className="field">
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
        <div className="field">
          <label htmlFor="reject-reason">Rejection reason (optional)</label>
          <input id="reject-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
      </div>
      {error && <p className="alert tone-fail" role="alert">{error}</p>}
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

function tone(e: RunEvent): Tone {
  if (e.type === "run_failed" || e.type === "step_failed" || e.type === "run_rejected") return "fail";
  if (e.type === "step_attempt_failed" || e.type === "approval_requested") return "gate";
  if (e.type === "run_interrupted") return "cut";
  if (e.type === "run_succeeded") return "ok";
  return "idle";
}

function summarize(e: RunEvent): string {
  const p = e.payload;
  const step = `step ${p.idx} ${p.name}`;
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
