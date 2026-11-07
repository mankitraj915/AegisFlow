"""The simulator drives the real FastAPI app, so the API path gets the same faults."""
from aegisflow.api import create_app
from aegisflow.sim import asgi
from aegisflow.sim.run import simulate
from aegisflow.sim.workload import make_plan

HTTP_WORLD = {"http_layer", "duplicate_commands", "readers", "stale_operators", "scenario_faults"}


def test_the_caller_reaches_the_real_routes_and_their_errors(db_path):
    app = create_app(db_path)

    assert asgi.call(app, "GET", "/health").body == {"status": "ok"}
    assert asgi.call(app, "GET", "/runs/nope").status == 404
    assert asgi.call(app, "POST", "/runs", {"order_id": "ORD-1001", "scenario": {"faults": {"x": []}}}).status == 422

    first = asgi.call(app, "POST", "/runs", {"order_id": "ORD-1001"}, {"Idempotency-Key": "k"})
    again = asgi.call(app, "POST", "/runs", {"order_id": "ORD-1001"}, {"Idempotency-Key": "k"})
    assert (first.status, again.status) == (201, 200)
    assert first.body["id"] == again.body["id"]


def test_a_seed_through_http_replays_exactly():
    for seed in range(10):
        first, second = simulate(seed, HTTP_WORLD), simulate(seed, HTTP_WORLD)
        assert first.digest == second.digest and first.dump == second.dump


def test_requests_really_go_through_the_app(monkeypatch):
    calls = []
    real = asgi.call

    def counting(app, method, path, body=None, headers=None):
        reply = real(app, method, path, body, headers)
        calls.append((method, path.split("/")[1], reply.status))
        return reply

    monkeypatch.setattr(asgi, "call", counting)
    assert simulate(1, HTTP_WORLD).failures() == []
    assert ("POST", "runs", 201) in calls


def test_a_process_can_die_inside_a_request_without_upsetting_anything():
    died = 0
    for seed in range(60):
        result = simulate(seed, {"http_layer", "crash", "power_loss", "tool_stall", "duplicate_commands"})
        died += result.stats["crash"] + result.stats["power_loss"]
        assert result.failures() == [], f"seed {seed}"
    assert died > 0


def test_some_requests_carry_no_idempotency_key():
    keys = {c.request_id for seed in range(30) for c in make_plan(seed, {"http_layer"}).commands if c.kind == "create"}
    assert "" in keys and keys - {""}
