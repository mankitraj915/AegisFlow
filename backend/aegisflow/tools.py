"""Deterministic mock tools. Money is integer paise everywhere.

Each tool is fn(conn, idempotency_key, state) -> output dict. Fatal failures
raise ToolError; transient ones raise RetryableToolError. issue_refund and notify_customer have side effects;
they write to the mock external system in its own committed transaction, keyed so
that a repeated call changes nothing, like a real provider's idempotency keys.

The engine invokes tools through call(), which applies the scenario's
deterministic fault plan for the given attempt, and asks what really happened
through lookup(), which never changes anything at the provider.
"""
import sqlite3
from collections.abc import Callable

from . import runtime
from .db import now, tx
from .demo_data import CUSTOMERS, ORDER_KINDS, ORDERS


class ToolError(Exception):
    """Fatal, non-retryable tool failure."""


class RetryableToolError(Exception):
    """Transient tool failure; the same step may be retried with the same key.
    Deliberately not a ToolError, so fatal handlers never swallow it."""


# Tools whose effects may have committed even when they raise.
SIDE_EFFECTING = {"issue_refund", "notify_customer"}


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
    remaining = order["total_paise"] - _ledger(conn).refunded_total(order["order_id"])
    if remaining <= 0:
        raise ToolError(f"order {order['order_id']} is already fully refunded")
    return {"eligible": True, "refundable_paise": remaining}


def propose_refund(conn, key, state):
    order = state["outputs"]["fetch_order"]
    policy = state["outputs"]["check_refund_policy"]
    return {
        "order_id": order["order_id"],
        "amount_paise": policy["refundable_paise"],
        "reason": "customer-requested refund of the remaining amount",
    }


class SqlLedger:
    """The mock payment system: a refunds table reached through one connection.
    Production uses the engine's own database; the simulator runs it separately."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def issue(self, key: str, order_id: str, amount: int) -> None:
        try:
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
        except sqlite3.IntegrityError as exc:
            raise ToolError(f"ledger refused {amount} paise for {order_id}: {exc}") from exc

    def refund_status(self, key: str) -> dict | None:
        """What the provider holds under this key, or None if it has no record."""
        row = self.conn.execute(
            "SELECT order_id, amount_paise FROM refunds WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def refunded_totals(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT order_id, SUM(amount_paise) AS total FROM refunds GROUP BY order_id")
        return {row["order_id"]: row["total"] for row in rows}

    def refunded_total(self, order_id: str) -> int:
        return self.refunded_totals().get(order_id, 0)

    def send_message(self, key: str, to: str, message: str) -> bool:
        """Deliver a message, once per key. Returns whether this call delivered it."""
        with tx(self.conn):
            return self.conn.execute(
                "INSERT OR IGNORE INTO notifications(idempotency_key, recipient, message, created_at) "
                "VALUES (?,?,?,?)",
                (key, to, message, now()),
            ).rowcount == 1

    def message_status(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT recipient, message FROM notifications WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None


def _ledger(conn):
    make = runtime.current.ledger
    return make(conn) if make else SqlLedger(conn)


def available_orders(conn) -> list[dict]:
    """The first order of each kind with something left to refund."""
    refunded = _ledger(conn).refunded_totals()
    found: dict[str, dict] = {}
    for order_id, kind in ORDER_KINDS.items():
        remaining = ORDERS[order_id]["total_paise"] - refunded.get(order_id, 0)
        if kind not in found and remaining > 0:
            found[kind] = {"id": order_id, "kind": kind, "remaining_paise": remaining}
    return list(found.values())


def _refund_output(key: str, state) -> dict:
    proposal = state["outputs"]["propose_refund"]
    return {"refund_id": key, "order_id": proposal["order_id"], "amount_paise": proposal["amount_paise"]}


def _message_output(key: str, state) -> dict:
    refund = state["outputs"]["issue_refund"]
    return {
        "channel": "email",
        "to": state["outputs"]["fetch_customer"]["email"],
        "message": f"Your refund of {refund['amount_paise']} paise for {refund['order_id']} has been issued.",
    }


def issue_refund(conn: sqlite3.Connection, key: str, state) -> dict:
    output = _refund_output(key, state)
    _ledger(conn).issue(key, output["order_id"], output["amount_paise"])
    return output


def notify_customer(conn, key, state):
    output = _message_output(key, state)
    _ledger(conn).send_message(key, output["to"], output["message"])
    return output


def _refund_status(conn, key, state) -> dict | None:
    record = _ledger(conn).refund_status(key)
    if record is None:
        return None
    output = _refund_output(key, state)
    if (record["order_id"], record["amount_paise"]) != (output["order_id"], output["amount_paise"]):
        raise ToolError(f"provider holds {record['order_id']}/{record['amount_paise']} under {key}, "
                        f"not {output['order_id']}/{output['amount_paise']}")
    return output


def _message_status(conn, key, state) -> dict | None:
    return _message_output(key, state) if _ledger(conn).message_status(key) else None


EXPECTED_OUTPUT: dict[str, Callable] = {"issue_refund": _refund_output, "notify_customer": _message_output}
STATUS: dict[str, Callable] = {"issue_refund": _refund_status, "notify_customer": _message_status}


TOOLS: dict[str, Callable] = {
    "fetch_order": fetch_order,
    "fetch_customer": fetch_customer,
    "check_refund_policy": check_refund_policy,
    "propose_refund": propose_refund,
    "issue_refund": issue_refund,
    "notify_customer": notify_customer,
}


def lookup(conn, name: str, key: str, state) -> dict | None:
    """Ask the provider what it did under this key: the step's output if the effect
    happened, None if it has no record of one. Raises if it can't say."""
    if state["scenario"].get("provider_status_down"):
        raise RetryableToolError("injected: provider status lookup unavailable")
    return runtime.current.lookup(name, key, lambda: STATUS[name](conn, key, state))


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
