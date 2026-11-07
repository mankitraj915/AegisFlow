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
}
# Workload shapes, switched per seed like faults.
SHAPES = ("duplicate_commands", "scenario_faults", "readers", "stale_operators")
FEATURES = tuple(FAULT_RATES) + SHAPES

ORDERS = ["ORD-1001", "ORD-1002", "ORD-2002", "ORD-3003", "ORD-4004", "ORD-9009"]
SCENARIO_FAULTS = ["retryable", "fatal", "retryable_after_commit", "interrupt_after_commit"]
LOW_VALUE_ORDER_WEIGHT = 4


@dataclass
class Command:
    kind: str  # create | operate | read
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
    rates = {f: rng.uniform(*span) for f, span in FAULT_RATES.items()}
    stickiness = rng.choice([0.0, 0.5, 0.9])
    if only is not None:
        enabled = set(only)
    enabled -= set(without)

    commands = []
    weights = [LOW_VALUE_ORDER_WEIGHT, 2, 2, 1, 1, 1]
    for i in range(rng.randint(1, 3)):
        order = rng.choices(ORDERS, weights)[0]
        scenario = _scenario(rng, "scenario_faults" in enabled)
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
    return Plan(enabled, rates, stickiness, commands)
