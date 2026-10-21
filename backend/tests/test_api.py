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


def test_error_contract(client):
    assert client.get("/runs/nope").status_code == 404
    assert client.get("/runs/nope/events").status_code == 404
    assert client.post("/runs/nope/approve", json={"decided_by": "x"}).status_code == 404
    assert client.post("/runs", json={"order_id": "NOPE"}).status_code == 404
    assert client.post("/runs", json={}).status_code == 422
    run = create(client, "ORD-2002")
    assert client.post(f"/runs/{run['id']}/approve", json={}).status_code == 422
    assert create(client, "ORD-9009")["status"] == "failed"
