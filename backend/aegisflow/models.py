from typing import Literal

from pydantic import BaseModel, Field, field_validator

from . import planner

DEFAULT_APPROVAL_THRESHOLD_PAISE = 500_000  # ₹5,000

FaultKind = Literal["retryable", "fatal", "retryable_after_commit"]


class Scenario(BaseModel):
    approval_threshold_paise: int = Field(DEFAULT_APPROVAL_THRESHOLD_PAISE, ge=0)
    require_manual_review: bool = False
    # Deterministic fault injection: step name -> outcome of attempts 1..n.
    # Attempts beyond the list run normally.
    faults: dict[str, list[FaultKind]] = Field(default_factory=dict)

    @field_validator("faults")
    @classmethod
    def _known_steps(cls, faults):
        unknown = sorted(set(faults) - set(planner.PIPELINE))
        if unknown:
            raise ValueError(f"unknown steps: {unknown}")
        return faults


class CreateRun(BaseModel):
    order_id: str
    scenario: Scenario = Field(default_factory=Scenario)


class Decision(BaseModel):
    decided_by: str = Field(min_length=1)
    reason: str | None = None


class RunOut(BaseModel):
    id: str
    order_id: str
    status: str
    scenario: Scenario
    error: str | None
    created_at: str
    updated_at: str


class StepOut(BaseModel):
    idx: int
    name: str
    status: str
    input: dict
    output: dict | None
    error: str | None
    attempt: int
    idempotency_key: str
    started_at: str
    finished_at: str | None


class ApprovalOut(BaseModel):
    step_idx: int
    status: str
    requested_payload: dict
    decided_by: str | None
    reason: str | None
    decided_at: str | None


class RunDetail(RunOut):
    steps: list[StepOut]
    pending_approval: ApprovalOut | None
    latest_checkpoint_idx: int | None


class EventOut(BaseModel):
    seq: int
    type: str
    payload: dict
    ts: str
