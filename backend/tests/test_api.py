import pytest
from fastapi.testclient import TestClient

from aegisflow.api import create_app, create_site


def create(client, order_id, **scenario):
    r = client.post("/runs", json={"order_id": order_id, "scenario": scenario})
    assert r.status_code == 201, r.text
    return r.json()


def test_low_value_run_end_to_end(client):
    run = create(client, "ORD-1001")
    assert run["status"] == "succeeded"
    detail = client.get(f"/runs/{run['id']}").json()
    assert [s["name"] for s in detail["steps"]][-2:] == ["issue_refund", "notify_customer"]
    assert detail["pending_approval"] is None and detail["latest_checkpoint_idx"] == 5
    types = [e["type"] for e in client.get(f"/runs/{run['id']}/events").json()]
    assert types[0] == "run_created" and types[-1] == "run_succeeded"
    assert [r["id"] for r in client.get("/runs").json()] == [run["id"]]


def test_approval_flow(client):
    run = create(client, "ORD-2002")
    assert run["status"] == "awaiting_approval"
    detail = client.get(f"/runs/{run['id']}").json()
    assert detail["pending_approval"]["status"] == "pending"
    assert detail["pending_approval"]["requested_payload"]["amount_paise"] == 1_200_000

    r = client.post(f"/runs/{run['id']}/approve", json={"decided_by": "ops"})
    assert r.status_code == 200 and r.json()["status"] == "succeeded"
    assert client.post(f"/runs/{run['id']}/approve", json={"decided_by": "ops"}).status_code == 409
    assert client.post(f"/runs/{run['id']}/reject", json={"decided_by": "ops"}).status_code == 409


def test_reject_flow(client):
    run = create(client, "ORD-2002")
    r = client.post(f"/runs/{run['id']}/reject", json={"decided_by": "ops", "reason": "fraud"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert client.post(f"/runs/{run['id']}/approve", json={"decided_by": "ops"}).status_code == 409


def test_scenario_threshold_over_api(client):
    assert create(client, "ORD-1001", approval_threshold_paise=1000)["status"] == "awaiting_approval"
    assert create(client, "ORD-3003")["status"] == "awaiting_approval"


def test_fault_injection_over_api(client):
    run = create(client, "ORD-1001", faults={"issue_refund": ["retryable_after_commit"]})
    assert run["status"] == "succeeded" and run["scenario"]["faults"] == {"issue_refund": ["retryable_after_commit"]}
    detail = client.get(f"/runs/{run['id']}").json()
    assert [s["attempt"] for s in detail["steps"]] == [1, 1, 1, 1, 2, 1]
    for bad in ({"nope": ["retryable"]}, {"fetch_order": ["flaky"]}):
        r = client.post("/runs", json={"order_id": "ORD-1001", "scenario": {"faults": bad}})
        assert r.status_code == 422


def test_interrupt_and_resume_over_api(client):
    run = create(client, "ORD-1001", faults={"issue_refund": ["interrupt_after_commit"]})
    assert (run["status"], run["interrupted"]) == ("running", True)
    assert [(r["status"], r["interrupted"]) for r in client.get("/runs").json()] == [("running", True)]
    detail = client.get(f"/runs/{run['id']}").json()
    stuck = detail["steps"][-1]
    assert (stuck["name"], stuck["status"]) == ("issue_refund", "started")
    key = stuck["idempotency_key"]
    assert [r["idempotency_key"] for r in detail["refunds"]] == [key]
    assert client.get(f"/runs/{run['id']}/events").json()[-1]["type"] == "run_interrupted"

    r = client.post(f"/runs/{run['id']}/resume")
    assert r.status_code == 200 and (r.json()["status"], r.json()["interrupted"]) == ("succeeded", False)
    detail = client.get(f"/runs/{run['id']}").json()
    assert detail["steps"][4]["idempotency_key"] == key and detail["steps"][4]["status"] == "succeeded"
    assert [s["name"] for s in detail["steps"]][-1] == "notify_customer"
    assert [(r["idempotency_key"], r["amount_paise"]) for r in detail["refunds"]] == [(key, 129_900)]
    types = [e["type"] for e in client.get(f"/runs/{run['id']}/events").json()]
    assert types.count("run_interrupted") == types.count("run_resumed") == 1 and types[-1] == "run_succeeded"
    assert client.post(f"/runs/{run['id']}/resume").status_code == 409
    assert client.post("/runs/nope/resume").status_code == 404


def test_repeated_request_with_a_key_gets_the_same_run(client):
    headers = {"Idempotency-Key": "client-request-1"}
    body = {"order_id": "ORD-1001"}
    first = client.post("/runs", json=body, headers=headers)
    second = client.post("/runs", json=body, headers=headers)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"]
    assert len(client.get("/runs").json()) == 1
    assert len(client.get(f"/runs/{first.json()['id']}").json()["refunds"]) == 1


def test_key_reused_for_a_different_request_conflicts(client):
    headers = {"Idempotency-Key": "client-request-1"}
    assert client.post("/runs", json={"order_id": "ORD-1001"}, headers=headers).status_code == 201
    assert client.post("/runs", json={"order_id": "ORD-2002"}, headers=headers).status_code == 409
    r = client.post("/runs", json={"order_id": "ORD-1001", "scenario": {"require_manual_review": True}},
                    headers=headers)
    assert r.status_code == 409
    assert len(client.get("/runs").json()) == 1


def test_a_repeat_returns_the_run_as_it_stands_without_driving_it(client):
    headers = {"Idempotency-Key": "k"}
    body = {"order_id": "ORD-1001", "scenario": {"faults": {"issue_refund": ["interrupt_after_commit"]}}}
    first = client.post("/runs", json=body, headers=headers).json()
    again = client.post("/runs", json=body, headers=headers).json()
    assert (first["status"], first["interrupted"]) == ("running", True)
    assert (again["id"], again["status"], again["interrupted"]) == (first["id"], "running", True)


def test_a_repeat_still_answers_at_the_run_cap(db_path):
    client = TestClient(create_app(db_path, max_runs=1))
    headers = {"Idempotency-Key": "k"}
    assert client.post("/runs", json={"order_id": "ORD-1001"}, headers=headers).status_code == 201
    assert client.post("/runs", json={"order_id": "ORD-1001"}, headers=headers).status_code == 200
    assert client.post("/runs", json={"order_id": "ORD-2002"}).status_code == 429


def test_key_is_bounded(client):
    r = client.post("/runs", json={"order_id": "ORD-1001"}, headers={"Idempotency-Key": "k" * 201})
    assert r.status_code == 422


def test_available_orders_move_past_refunded_ones(client):
    def first_of(kind):
        return next(o for o in client.get("/orders/available").json() if o["kind"] == kind)

    assert first_of("under_threshold") == {"id": "ORD-1001", "kind": "under_threshold", "remaining_paise": 129_900}
    assert client.post("/runs", json={"order_id": "ORD-1001"}).json()["status"] == "succeeded"
    assert first_of("under_threshold")["id"] == "ORD-1002"
    assert first_of("cancelled")["id"] == "ORD-9009"


def test_error_contract(client):
    assert client.get("/runs/nope").status_code == 404
    assert client.get("/runs/nope/events").status_code == 404
    assert client.post("/runs/nope/approve", json={"decided_by": "x"}).status_code == 404
    assert client.post("/runs", json={"order_id": "NOPE"}).status_code == 404
    assert client.post("/runs", json={}).status_code == 422
    run = create(client, "ORD-2002")
    assert client.post(f"/runs/{run['id']}/approve", json={}).status_code == 422
    assert create(client, "ORD-9009")["status"] == "failed"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_run_cap_bounds_public_demo(db_path):
    client = TestClient(create_app(db_path, max_runs=2))
    create(client, "ORD-1001")
    create(client, "ORD-2002")
    r = client.post("/runs", json={"order_id": "ORD-1001"})
    assert r.status_code == 429 and "limited to 2 runs" in r.json()["detail"]
    assert len(client.get("/runs").json()) == 2
    # Existing runs stay fully usable at the cap.
    run_id = client.get("/runs").json()[1]["id"]
    assert client.post(f"/runs/{run_id}/approve", json={"decided_by": "ops"}).json()["status"] == "succeeded"


def test_input_bounds(client):
    assert client.post("/runs", json={"order_id": "X" * 65}).status_code == 422
    r = client.post("/runs", json={"order_id": "ORD-1001", "scenario": {"faults": {"fetch_order": ["fatal"] * 11}}})
    assert r.status_code == 422
    run = create(client, "ORD-2002")
    assert client.post(f"/runs/{run['id']}/approve", json={"decided_by": "x" * 101}).status_code == 422
    assert client.post(f"/runs/{run['id']}/reject", json={"decided_by": "ops", "reason": "r" * 501}).status_code == 422
    assert client.get(f"/runs/{run['id']}").json()["status"] == "awaiting_approval"


def test_site_serves_frontend_and_api_on_one_origin(db_path, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    site = TestClient(create_site(db_path, str(dist)))
    assert site.get("/").text == "<div id=root></div>"
    assert site.get("/assets/app.js").status_code == 200
    assert site.get("/api/health").json() == {"status": "ok"}
    r = site.post("/api/runs", json={"order_id": "ORD-1001"})
    assert r.status_code == 201 and r.json()["status"] == "succeeded"
    assert site.get(f"/api/runs/{r.json()['id']}/events").status_code == 200
    assert site.get("/api/runs/nope").status_code == 404


def test_site_refuses_to_start_without_frontend_build(db_path, tmp_path):
    with pytest.raises(RuntimeError, match="frontend build not found"):
        create_site(db_path, str(tmp_path / "missing"))
