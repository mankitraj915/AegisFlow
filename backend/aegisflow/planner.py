"""Deterministic planner and approval policy. Pure functions of run state."""

PIPELINE = [
    "fetch_order",
    "fetch_customer",
    "check_refund_policy",
    "propose_refund",
    "issue_refund",
    "notify_customer",
]


def decide(state: dict) -> str | None:
    """Next step name, or None when the run is complete."""
    for name in PIPELINE:
        if name not in state["outputs"]:
            return name
    return None


def approval_reason(step_name: str, state: dict) -> str | None:
    """Why this step needs human approval, or None. Only a refund that exceeds
    the scenario threshold, or one under explicit manual review, is gated."""
    if step_name != "issue_refund":
        return None
    scenario = state["scenario"]
    amount = state["outputs"]["propose_refund"]["amount_paise"]
    if amount > scenario["approval_threshold_paise"]:
        return f"refund {amount} paise exceeds threshold {scenario['approval_threshold_paise']} paise"
    if scenario["require_manual_review"] or state["outputs"]["fetch_order"]["manual_review"]:
        return "manual review required"
    return None
