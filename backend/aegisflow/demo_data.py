"""The demo's mock orders and customers.

An order can be refunded only up to its total, so a public demo needs a pool of
orders rather than one of each kind. The first of each kind keeps the id the
scenarios are documented with.
"""

CUSTOMERS = {
    "CUST-1": {"name": "Asha Rao", "email": "asha@example.test"},
    "CUST-2": {"name": "Vikram Shah", "email": "vikram@example.test"},
}

# kind -> (first order number, how many, the order). Most presets use the first
# kind, so it matches the demo's 500-run limit (AEGISFLOW_MAX_RUNS in render.yaml).
_SERIES = {
    # Low value, well under the default threshold.
    "under_threshold": (1001, 500, {"customer_id": "CUST-1", "total_paise": 129_900, "status": "delivered",
                                    "manual_review": False}),
    # High value: exceeds the default ₹5,000 threshold.
    "over_threshold": (2002, 100, {"customer_id": "CUST-2", "total_paise": 1_200_000, "status": "delivered",
                                   "manual_review": False}),
    # Low value but flagged for manual review on the order itself.
    "manual_review": (3003, 50, {"customer_id": "CUST-1", "total_paise": 49_900, "status": "delivered",
                                 "manual_review": True}),
    # Exactly at the threshold: "exceeds" is strict, so no approval.
    "at_threshold": (4004, 50, {"customer_id": "CUST-2", "total_paise": 500_000, "status": "delivered",
                                "manual_review": False}),
    # Not refundable.
    "cancelled": (9009, 1, {"customer_id": "CUST-1", "total_paise": 89_900, "status": "cancelled",
                            "manual_review": False}),
}

ORDER_KINDS = {
    f"ORD-{number}": kind
    for kind, (first, count, _) in _SERIES.items()
    for number in range(first, first + count)
}
ORDERS = {order_id: dict(_SERIES[kind][2]) for order_id, kind in ORDER_KINDS.items()}
