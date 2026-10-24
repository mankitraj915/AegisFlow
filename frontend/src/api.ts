// Typed client for the backend HTTP API (backend/aegisflow/api.py, models.py).
// Requests go to /api, which the Vite dev/preview server proxies to the backend.

export type RunStatus = "running" | "succeeded" | "failed" | "awaiting_approval" | "rejected";
export type FaultKind = "retryable" | "fatal" | "retryable_after_commit" | "interrupt_after_commit";

// Mirrors planner.PIPELINE; the backend rejects fault plans for other step names.
export const PIPELINE = [
  "fetch_order",
  "fetch_customer",
  "check_refund_policy",
  "propose_refund",
  "issue_refund",
  "notify_customer",
] as const;

export interface Scenario {
  approval_threshold_paise: number;
  require_manual_review: boolean;
  faults: Record<string, FaultKind[]>;
}

export interface Run {
  id: string;
  order_id: string;
  status: RunStatus;
  interrupted: boolean; // Running, stopped by a simulated interruption, not yet resumed.
  scenario: Scenario;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface Step {
  idx: number;
  name: string;
  status: "started" | "succeeded" | "failed";
  input: Record<string, unknown>;
  output: Record<string, unknown> | null;
  error: string | null;
  attempt: number;
  idempotency_key: string;
  started_at: string;
  finished_at: string | null;
}

export interface Approval {
  step_idx: number;
  status: string;
  requested_payload: { step: string; reason: string; amount_paise: number };
  decided_by: string | null;
  reason: string | null;
  decided_at: string | null;
}

// A row in the mock payment ledger, written under one of the run's step keys.
export interface Refund {
  idempotency_key: string;
  order_id: string;
  amount_paise: number;
  created_at: string;
}

export interface RunDetail extends Run {
  steps: Step[];
  pending_approval: Approval | null;
  latest_checkpoint_idx: number | null;
  refunds: Refund[];
}

export interface RunEvent {
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  ts: string;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, { headers: { "Content-Type": "application/json" }, ...init });
  } catch {
    throw new ApiError(0, "Cannot reach the API. Is the backend running?");
  }
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // Non-JSON body, e.g. the proxy's error page when the backend is down.
  }
  if (!res.ok) throw new ApiError(res.status, errorMessage(res.status, body));
  return body as T;
}

function errorMessage(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc: ["body", "scenario", ...], msg}, ...]
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`)
      .join("; ");
  }
  if (status >= 500) return `Server error (HTTP ${status}). Is the backend running on port 8000?`;
  return `Request failed (HTTP ${status}).`;
}

const post = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export const api = {
  listRuns: () => request<Run[]>("/runs"),
  getRun: (id: string) => request<RunDetail>(`/runs/${encodeURIComponent(id)}`),
  listEvents: (id: string) => request<RunEvent[]>(`/runs/${encodeURIComponent(id)}/events`),
  createRun: (order_id: string, scenario: Scenario) => request<Run>("/runs", post({ order_id, scenario })),
  approve: (id: string, decided_by: string) =>
    request<Run>(`/runs/${encodeURIComponent(id)}/approve`, post({ decided_by })),
  resume: (id: string) => request<Run>(`/runs/${encodeURIComponent(id)}/resume`, { method: "POST" }),
  reject: (id: string, decided_by: string, reason: string | null) =>
    request<Run>(`/runs/${encodeURIComponent(id)}/reject`, post({ decided_by, reason })),
};

export function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
