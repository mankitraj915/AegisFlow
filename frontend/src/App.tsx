import { useCallback, useEffect, useState } from "react";
import { api, messageOf, PIPELINE, type Run } from "./api";
import { CreateRunForm } from "./CreateRunForm";
import { Ident, STEP_INFO } from "./format";
import { RunDetail } from "./RunDetail";
import { RunList } from "./RunList";

// The selected run lives in the URL hash (#/runs/<id>) so it survives reloads and can be linked.
const idFromHash = () => /^#\/runs\/(.+)$/.exec(location.hash)?.[1] ?? null;

export default function App() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(idFromHash);

  const loadRuns = useCallback(async () => {
    setListError(null);
    try {
      setRuns(await api.listRuns());
    } catch (err) {
      setListError(messageOf(err));
    }
  }, []);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  useEffect(() => {
    const onHash = () => setSelectedId(idFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const select = (id: string) => {
    location.hash = `/runs/${id}`;
    // In the single-column layout the run detail sits below the form; bring it into view.
    if (matchMedia("(max-width: 1099px)").matches) {
      const smooth = !matchMedia("(prefers-reduced-motion: reduce)").matches;
      requestAnimationFrame(() =>
        document.getElementById("run-view")?.scrollIntoView({ behavior: smooth ? "smooth" : "auto" }),
      );
    }
  };

  return (
    <div className="shell">
      <header className="masthead">
        <div className="brand">
          <h1>AegisFlow</h1>
          <span className="brand-sub">refund workflow runtime</span>
        </div>
        <p className="lede">
          Each refund runs as six recorded steps. Choose a scenario to retry, interrupt, approve or resume a run, then
          check the evidence: every step gets one idempotency key, so the ledger records a refund at most once.
        </p>
        <p className="disclosure">
          <strong>Shared simulation.</strong> No money moves and no messages are sent. Every visitor sees and can act
          on the same runs, and the free server deletes all data when it restarts.
        </p>
      </header>

      <main className="layout">
        <div className="side">
          <CreateRunForm
            onCreated={(run) => {
              loadRuns();
              select(run.id);
            }}
          />
          <RunList runs={runs} error={listError} selectedId={selectedId} onSelect={select} onReload={loadRuns} />
        </div>
        <div id="run-view" className="view">
          {selectedId ? <RunDetail runId={selectedId} onChanged={loadRuns} /> : <Overview />}
        </div>
      </main>
    </div>
  );
}

function Overview() {
  return (
    <section className="overview" aria-labelledby="overview-title">
      <p className="label">How a run executes</p>
      <h2 id="overview-title" className="view-title">Six steps, each one on the record</h2>
      <div className="trace">
        <ol className="rail">
          {PIPELINE.map((name, idx) => (
            <li key={name} className="station state-pending">
              <div className="station-body">
                <span className="node" aria-hidden="true" />
                <span className="station-index">{idx}</span>
                <span className="station-title">{STEP_INFO[name].title}</span>
                <span className="station-id mono"><Ident>{name}</Ident></span>
                <span className="station-note">{STEP_INFO[name].does}</span>
                {name === "issue_refund" && (
                  <span className="station-gate">Pauses for approval above the threshold or under manual review.</span>
                )}
              </div>
            </li>
          ))}
        </ol>
      </div>
      <dl className="guarantees">
        <div>
          <dt>One key per step</dt>
          <dd>Step <i>n</i> of a run uses <code>{"{run id}:{n}"}</code>. Retries and resumes reuse it, and the ledger replays a key it already has.</dd>
        </div>
        <div>
          <dt>Bounded retries</dt>
          <dd>A transient fault is retried up to 3 attempts in total, 0.1 s and then 0.2 s apart. A fatal fault fails the run at once.</dd>
        </div>
        <div>
          <dt>Ordered evidence</dt>
          <dd>Every transition is appended to the run's event log, next to the ledger rows written under its keys.</dd>
        </div>
      </dl>
      <p className="muted">Choose a scenario and start a run, or select an existing run.</p>
    </section>
  );
}
