import type { Run } from "./api";
import { formatTime, shortId, StatusBadge } from "./format";

interface Props {
  runs: Run[] | null;
  error: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onReload: () => void;
}

export function RunList({ runs, error, selectedId, onSelect, onReload }: Props) {
  return (
    <section className="panel" aria-labelledby="runs-title">
      <div className="panel-head">
        <h2 id="runs-title">Runs</h2>
        <button type="button" onClick={onReload}>Refresh</button>
      </div>
      {error ? (
        <p className="alert alert-error" role="alert">{error}</p>
      ) : runs === null ? (
        <p className="muted" aria-live="polite">Loading runs…</p>
      ) : runs.length === 0 ? (
        <p className="muted">No runs yet. Start one above.</p>
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
                <span className="run-item-main">
                  <span className="mono">{run.order_id}</span>
                  <StatusBadge status={run.interrupted ? "interrupted" : run.status} />
                </span>
                <span className="run-item-meta">
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
