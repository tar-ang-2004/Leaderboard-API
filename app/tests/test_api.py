import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


@pytest.fixture
def lb_id():
    r = client.post("/v1/leaderboards", json={"name": "test-board"})
    assert r.status_code == 201
    return r.json()["id"]


def test_health():
    assert client.get("/health").status_code == 200


def test_create_and_delete_leaderboard():
    r = client.post("/v1/leaderboards", json={"name": "my-board"})
    assert r.status_code == 201
    lb_id = r.json()["id"]
    assert client.delete(f"/v1/leaderboards/{lb_id}").status_code == 204


def test_submit_and_rank(lb_id):
    client.post(f"/v1/leaderboards/{lb_id}/scores", json={"player_id": "alice", "score": 500})
    client.post(f"/v1/leaderboards/{lb_id}/scores", json={"player_id": "bob",   "score": 800})
    r = client.get(f"/v1/leaderboards/{lb_id}/players/alice/rank")
    assert r.status_code == 200
    assert r.json()["rank"] == 2


def test_top_k(lb_id):
    for i in range(5):
        client.post(f"/v1/leaderboards/{lb_id}/scores", json={"player_id": f"p{i}", "score": i * 100})
    r = client.get(f"/v1/leaderboards/{lb_id}/top?k=3")
    assert r.status_code == 200
    assert len(r.json()) == 3
    assert r.json()[0]["rank"] == 1


def test_update_score(lb_id):
    client.post(f"/v1/leaderboards/{lb_id}/scores", json={"player_id": "alice", "score": 100})
    client.post(f"/v1/leaderboards/{lb_id}/scores", json={"player_id": "alice", "score": 999})
    r = client.get(f"/v1/leaderboards/{lb_id}/players/alice/rank")
    assert r.json()["score"] == 999.0
    assert r.json()["rank"] == 1


def test_remove_player(lb_id):
    client.post(f"/v1/leaderboards/{lb_id}/scores", json={"player_id": "alice", "score": 100})
    r = client.delete(f"/v1/leaderboards/{lb_id}/scores/alice")
    assert r.status_code == 200
    assert r.json()["removed"] is True


def test_range(lb_id):
    for i in range(10):
        client.post(f"/v1/leaderboards/{lb_id}/scores", json={"player_id": f"p{i}", "score": float(i * 10)})
    r = client.get(f"/v1/leaderboards/{lb_id}/range?from_rank=1&to_rank=5")
    assert r.status_code == 200
    assert len(r.json()) == 5
