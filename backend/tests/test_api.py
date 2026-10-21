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
