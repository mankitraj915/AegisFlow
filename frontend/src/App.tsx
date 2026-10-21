import { useCallback, useEffect, useState } from "react";
import { api, messageOf, type Run } from "./api";
import { CreateRunForm } from "./CreateRunForm";
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
  };

  return (
    <div className="app">
      <header className="topbar">
        <h1>AegisFlow</h1>
        <span className="muted">Refund workflow runs</span>
      </header>
      <p className="notice">
        Public demo. Orders, refunds and notifications are simulated: no money moves and no messages are sent.
        Everyone shares the same runs, and data is kept only until the server restarts.
      </p>
      <main className="layout">
        <div className="sidebar">
          <CreateRunForm
            onCreated={(run) => {
              loadRuns();
              select(run.id);
            }}
          />
          <RunList runs={runs} error={listError} selectedId={selectedId} onSelect={select} onReload={loadRuns} />
        </div>
        {selectedId ? (
          <RunDetail runId={selectedId} onChanged={loadRuns} />
        ) : (
          <section className="panel guide" aria-labelledby="guide-title">
            <h2 id="guide-title">What this shows</h2>
            <p>
              Each run is a persisted state machine over six steps, from <span className="mono">fetch_order</span> to{" "}
              <span className="mono">notify_customer</span>. Every step records its input, output, attempts and
              idempotency key, and every transition is appended to an ordered event log.
            </p>
            <h3>Try</h3>
            <ul>
              <li><span className="mono">ORD-1001</span>: completes all six steps without approval.</li>
              <li>
                <span className="mono">ORD-2002</span>: ₹12,000 is over the ₹5,000 threshold, so the run pauses
                before the refund until you approve or reject it.
              </li>
              <li>
                <span className="mono">ORD-1001</span> with <em>Times out after commit, once</em> on{" "}
                <span className="mono">issue_refund</span>: the refund commits, the step fails, and the retry reuses
                the same idempotency key, so the simulated ledger records the refund only once.
              </li>
              <li>
                <span className="mono">ORD-1001</span> with <em>Times out after commit, every attempt</em>: retries run
                out and the run ends as <strong>Outcome unknown</strong>.
              </li>
              <li><span className="mono">ORD-9009</span>: a cancelled order fails the refund policy check.</li>
            </ul>
            <p className="muted">Select a run to see its steps, events and approval state.</p>
          </section>
        )}
      </main>
    </div>
  );
}
