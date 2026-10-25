import type { Run } from "./api";
import { displayStatus, formatTime, shortId, StatusMark } from "./format";

interface Props {
  runs: Run[] | null;
  error: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onReload: () => void;
}

export function RunList({ runs, error, selectedId, onSelect, onReload }: Props) {
  return (
    <section className="runs" aria-labelledby="runs-title">
      <div className="section-head">
        <h2 id="runs-title" className="section-title">
          Runs{runs && runs.length > 0 && <span className="count"> {runs.length}</span>}
        </h2>
        <button type="button" className="quiet" onClick={onReload}>Refresh</button>
      </div>
      {error ? (
        <p className="alert tone-fail" role="alert">{error}</p>
      ) : runs === null ? (
        <p className="muted" aria-live="polite">Loading runs…</p>
      ) : runs.length === 0 ? (
        <p className="muted">No runs yet. Choose a scenario and start one.</p>
      ) : (
        <ul className="run-list">
          {/* The API lists oldest first; show newest first. */}
          {[...runs].reverse().map((run) => (
            <li key={run.id}>
              <button
                type="button"
                className="run-item"
                aria-current={run.id === selectedId ? "true" : undefined}
                onClick={() => onSelect(run.id)}
              >
                <span className="mono run-order">{run.order_id}</span>
                <StatusMark status={displayStatus(run)} />
                <span className="run-meta">
                  <span className="mono">{shortId(run.id)}</span> · {formatTime(run.created_at)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
