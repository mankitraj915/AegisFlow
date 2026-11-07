"""Deterministic mock tools. Money is integer paise everywhere.

Each tool is fn(conn, idempotency_key, state) -> output dict. Fatal failures
raise ToolError; transient ones raise RetryableToolError. issue_refund and notify_customer have side effects;
they write to the mock external system in its own committed transaction, keyed so
that a repeated call changes nothing, like a real provider's idempotency keys.

The engine invokes tools through call(), which applies the scenario's
deterministic fault plan for the given attempt.
"""
import sqlite3
from collections.abc import Callable

from . import runtime
from .db import now, tx


class ToolError(Exception):
    """Fatal, non-retryable tool failure."""


class RetryableToolError(Exception):
    """Transient tool failure; the same step may be retried with the same key.
    Deliberately not a ToolError, so fatal handlers never swallow it."""


# Tools whose effects may have committed even when they raise.
SIDE_EFFECTING = {"issue_refund", "notify_customer"}


ORDERS = {
    # Low value, well under the default threshold.
    "ORD-1001": {"customer_id": "CUST-1", "total_paise": 129_900, "status": "delivered", "manual_review": False},
    # High value: exceeds the default ₹5,000 threshold.
    "ORD-2002": {"customer_id": "CUST-2", "total_paise": 1_200_000, "status": "delivered", "manual_review": False},
    # Low value but flagged for manual review on the order itself.
    "ORD-3003": {"customer_id": "CUST-1", "total_paise": 49_900, "status": "delivered", "manual_review": True},
    # Exactly at the threshold: "exceeds" is strict, so no approval.
    "ORD-4004": {"customer_id": "CUST-2", "total_paise": 500_000, "status": "delivered", "manual_review": False},
    # Not refundable.
    "ORD-9009": {"customer_id": "CUST-1", "total_paise": 89_900, "status": "cancelled", "manual_review": False},
}

CUSTOMERS = {
    "CUST-1": {"name": "Asha Rao", "email": "asha@example.test"},
    "CUST-2": {"name": "Vikram Shah", "email": "vikram@example.test"},
}


def order_exists(order_id: str) -> bool:
    return order_id in ORDERS


def fetch_order(conn, key, state):
    order = ORDERS.get(state["order_id"])
    if order is None:
        raise ToolError(f"order not found: {state['order_id']}")
    return {"order_id": state["order_id"], **order}


def fetch_customer(conn, key, state):
    order = state["outputs"]["fetch_order"]
    customer = CUSTOMERS.get(order["customer_id"])
    if customer is None:
        raise ToolError(f"customer not found: {order['customer_id']}")
    return {"customer_id": order["customer_id"], **customer}


def check_refund_policy(conn, key, state):
    order = state["outputs"]["fetch_order"]
    if order["status"] != "delivered":
        raise ToolError(f"order {order['order_id']} is {order['status']}; not refundable")
    return {"eligible": True, "refundable_paise": order["total_paise"]}


def propose_refund(conn, key, state):
    order = state["outputs"]["fetch_order"]
    policy = state["outputs"]["check_refund_policy"]
    return {
        "order_id": order["order_id"],
        "amount_paise": policy["refundable_paise"],
        "reason": "customer-requested full refund",
    }


class SqlLedger:
    """The mock payment system: a refunds table reached through one connection.
    Production uses the engine's own database; the simulator runs it separately."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def issue(self, key: str, order_id: str, amount: int) -> None:
        with tx(self.conn):
            row = self.conn.execute(
                "SELECT order_id, amount_paise FROM refunds WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO refunds(idempotency_key, order_id, amount_paise, created_at) VALUES (?,?,?,?)",
                    (key, order_id, amount, now()),
                )
            elif (row["order_id"], row["amount_paise"]) != (order_id, amount):
                raise ToolError(
                    f"idempotency key {key} already used for {row['order_id']}/{row['amount_paise']}, "
                    f"refusing {order_id}/{amount}"
                )

    def send_message(self, key: str, to: str, message: str) -> bool:
        """Deliver a message, once per key. Returns whether this call delivered it."""
        with tx(self.conn):
            return self.conn.execute(
                "INSERT OR IGNORE INTO notifications(idempotency_key, recipient, message, created_at) "
                "VALUES (?,?,?,?)",
                (key, to, message, now()),
            ).rowcount == 1


def _ledger(conn):
    make = runtime.current.ledger
    return make(conn) if make else SqlLedger(conn)


def issue_refund(conn: sqlite3.Connection, key: str, state) -> dict:
    proposal = state["outputs"]["propose_refund"]
    order_id, amount = proposal["order_id"], proposal["amount_paise"]
    _ledger(conn).issue(key, order_id, amount)
    return {"refund_id": key, "order_id": order_id, "amount_paise": amount}


def notify_customer(conn, key, state):
    customer = state["outputs"]["fetch_customer"]
    refund = state["outputs"]["issue_refund"]
    message = f"Your refund of {refund['amount_paise']} paise for {refund['order_id']} has been issued."
    _ledger(conn).send_message(key, customer["email"], message)
    return {"channel": "email", "to": customer["email"], "message": message}


TOOLS: dict[str, Callable] = {
    "fetch_order": fetch_order,
    "fetch_customer": fetch_customer,
    "check_refund_policy": check_refund_policy,
    "propose_refund": propose_refund,
    "issue_refund": issue_refund,
    "notify_customer": notify_customer,
}


def call(conn, name: str, key: str, state, attempt: int) -> dict:
    """Run tool `name`, applying the injected fault (if any) for this attempt.
    'retryable_after_commit' runs the tool first, so its effects commit, then
    fails: a timeout after the external system already processed the call.
    'interrupt_after_commit' runs the tool normally here; the engine then stops
    the run before recording the step (see engine._interrupt_planned)."""
    plan = state["scenario"].get("faults", {}).get(name, [])
    fault = plan[attempt - 1] if attempt <= len(plan) else None
    if fault == "fatal":
        raise ToolError(f"injected fatal fault: {name} attempt {attempt}")
    if fault == "retryable":
        raise RetryableToolError(f"injected transient fault: {name} attempt {attempt}")
    output = runtime.current.invoke(name, key, lambda: TOOLS[name](conn, key, state))
    if fault == "retryable_after_commit":
        raise RetryableToolError(f"injected timeout after commit: {name} attempt {attempt}")
    return output
