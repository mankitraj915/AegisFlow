"""What one seed asks of the system: the commands clients send, and which faults
are switched on. Each fault has its own random stream, so turning one off leaves
every other fault's decisions unchanged, which is what makes shrinking work."""
import random
from dataclasses import dataclass, field

from .. import planner

# Per-call or per-step chance when a fault is on, as (low, high) for the seed to draw from.
FAULT_RATES: dict[str, tuple[float, float]] = {
    "crash": (0.002, 0.03),
    "power_loss": (0.001, 0.01),
    "torn_commit": (0.005, 0.03),
    "io_error": (0.002, 0.03),
    "commit_lost": (0.005, 0.05),
    "commit_unknown": (0.005, 0.05),
    "clock_jump": (0.002, 0.02),
    "tool_drop_request": (0.03, 0.3),
    "tool_drop_response": (0.03, 0.3),
    "tool_fatal": (0.02, 0.2),
    "tool_duplicate": (0.03, 0.3),
    "tool_delay": (0.03, 0.3),
    "tool_stall": (0.02, 0.2),
    "tool_late_delivery": (0.03, 0.2),
    # A less forgiving provider: it reports an error for a request it executed, answers
    # a retry with the failure it gave the earlier request, and can't always say what it did.
    "provider_false_error": (0.03, 0.3),
    "provider_stale_response": (0.1, 0.5),
    "status_unavailable": (0.03, 0.3),
}
# Workload shapes, switched per seed like faults.
SHAPES = ("duplicate_commands", "scenario_faults", "readers", "stale_operators", "provider_forgets",
          "recovery_worker", "poison_step")
# Newer features draw from a stream of their own, so adding one leaves each seed's other draws alone.
SIDE_FEATURES = ("provider_false_error", "provider_stale_response", "status_unavailable", "provider_forgets",
                 "recovery_worker", "poison_step")
FEATURES = tuple(f for f in (*FAULT_RATES, *SHAPES) if f not in SIDE_FEATURES)
# How long a forgetful provider remembers a key, and how long an outage can age a run.
RETENTION_S = (310, 700)
LONG_OUTAGE_S = 800

ORDERS = ["ORD-1001", "ORD-1002", "ORD-2002", "ORD-3003", "ORD-4004", "ORD-9009"]
SCENARIO_FAULTS = ["retryable", "fatal", "retryable_after_commit", "interrupt_after_commit"]
LOW_VALUE_ORDER_WEIGHT = 4


@dataclass
class Command:
    kind: str  # create | operate | read | sweep
    at_us: int
    order_id: str = ""
    scenario: dict = field(default_factory=dict)
    request_id: str = ""
    action: str = ""  # operate: resume | advance | approve | reject
    pick: float = 0.0  # operate/read: which run, as a fraction of those eligible
    duplicate: bool = False
    stale: bool = False  # operate: acts on any run, like a client with an out-of-date page


@dataclass
class Plan:
    enabled: set[str]
    rates: dict[str, float]
    stickiness: float
    commands: list[Command]
    retention_us: int = 0
    outage_us: int = 0
    poison: tuple[str, str] | None = None  # (order, step) that kills whoever runs it, every time.


def _when(rng: random.Random) -> int:
    window = rng.choice([50_000, 500_000, 5_000_000, 130_000_000])
    return rng.randrange(window)


def _scenario(rng: random.Random, with_faults: bool) -> dict:
    scenario: dict = {}
    threshold = rng.random()
    if threshold < 0.15:
        scenario["approval_threshold_paise"] = 0
    elif threshold < 0.3:
        scenario["approval_threshold_paise"] = 10_000_000
    if rng.random() < 0.15:
        scenario["require_manual_review"] = True
    faults = {}
    for name in planner.PIPELINE:
        if rng.random() < 0.2:
            faults[name] = [rng.choice(SCENARIO_FAULTS) for _ in range(rng.randint(1, 3))]
    if with_faults and faults:
        scenario["faults"] = faults
    return scenario


def make_plan(seed: int, only: set[str] | None = None, without: set[str] | frozenset[str] = frozenset()) -> Plan:
    rng = random.Random(f"{seed}:plan")
    # Swarm testing: each seed runs a random subset of faults, so one fault's
    # failures can't always mask the rarer path that another fault needs.
    enabled = {f for f in FEATURES if rng.random() < 0.4}
    rates = {f: rng.uniform(*span) for f, span in FAULT_RATES.items() if f not in SIDE_FEATURES}
    stickiness = rng.choice([0.0, 0.5, 0.9])
    side = random.Random(f"{seed}:side")
    rates |= {f: side.uniform(*span) for f, span in FAULT_RATES.items() if f in SIDE_FEATURES}
    enabled |= {f for f in SIDE_FEATURES if side.random() < 0.4}
    if only is not None:
        enabled = set(only)
    enabled -= set(without)
    retention_us = side.randrange(*RETENTION_S) * 1_000_000
    outage_us = side.randrange(LONG_OUTAGE_S) * 1_000_000 if "provider_forgets" in enabled else 0

    commands = []
    weights = [LOW_VALUE_ORDER_WEIGHT, 2, 2, 1, 1, 1]
    for i in range(rng.randint(1, 3)):
        order = rng.choices(ORDERS, weights)[0]
        scenario = _scenario(rng, "scenario_faults" in enabled)
        if "scenario_faults" in enabled and side.random() < 0.1:
            scenario["provider_status_down"] = True
        at = _when(rng)
        commands.append(Command("create", at, order, scenario, request_id=f"req-{i}"))
        retried = rng.random() < 0.5
        delay = _when(rng)
        if retried and "duplicate_commands" in enabled:
            commands.append(Command("create", at + delay, order, scenario, request_id=f"req-{i}", duplicate=True))
    for _ in range(rng.randint(0, 4)):
        action = rng.choice(["resume", "advance", "approve", "reject"])
        stale = rng.random() < 0.5 and "stale_operators" in enabled
        commands.append(Command("operate", _when(rng), action=action, pick=rng.random(), stale=stale))
    readers = rng.randint(1, 2)
    if "readers" in enabled:
        for _ in range(readers):
            commands.append(Command("read", _when(rng), pick=rng.random()))
    # After every other draw, so these leave the rest of the seed where it was.
    sweeps = side.randint(1, 3)
    if "recovery_worker" in enabled:
        for _ in range(sweeps):
            commands.append(Command("sweep", _when(side)))
    poison = (side.choices(ORDERS, weights)[0], side.choice(planner.PIPELINE))
    return Plan(enabled, rates, stickiness, commands, retention_us, outage_us,
                poison if "poison_step" in enabled else None)
