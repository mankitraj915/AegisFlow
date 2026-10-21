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
          <section className="panel empty-detail">
            <p className="muted">Select a run to see its steps, events and approval state.</p>
          </section>
        )}
      </main>
    </div>
  );
}
