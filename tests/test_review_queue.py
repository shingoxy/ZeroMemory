from pathlib import Path

from zero_memory.core.store import Store
from zero_memory.review.tables import initialized

from .conftest import propose
from .review_support import RUN_A, candidate, client_token, seed_messages


def test_queue_orders_by_priority_and_folds_low_priority(app, client):
    msgs = seed_messages(client, ["我喜欢用纸质笔记", "我习惯晚上写代码", "我偶尔去爬山"])
    high, mid = candidate(app, msgs[0], confidence=0.9), candidate(app, msgs[1], confidence=0.6)
    low = candidate(app, msgs[2], confidence=0.3)
    data = client.get("/v1/review/queue").json()
    assert [i["memory_id"] for i in data["items"]] == [high["memory_id"], mid["memory_id"]]
    assert data["counts"] == {"active": 2, "deferred": 0, "low_priority": 1, "sensitive": 0}
    assert data["items"][0]["review"]["priority"] == 0.9
    assert data["items"][0]["review"]["run"] == {"run_id": RUN_A, "kind": "batch"}
    folded = client.get("/v1/review/queue", params={"view": "low_priority"}).json()["items"]
    assert [i["memory_id"] for i in folded] == [low["memory_id"]]
    assert client.get("/v1/review/queue", params={"threshold": 0.2}).json()["counts"]["low_priority"] == 0


def test_defer_hides_until_undone_without_new_version(app, client):
    m = candidate(app, seed_messages(client, ["我每天早上跑步"])[0])
    assert client.post("/v1/review/defer", json={"memory_ids": [m["memory_id"]]}).json() == {"updated": 1, "undo": False}
    assert client.get("/v1/review/queue").json()["items"] == []
    [item] = client.get("/v1/review/queue", params={"view": "deferred"}).json()["items"]
    assert item["review"]["deferred"] is True and item["version"] == m["version"]
    client.post("/v1/review/defer", json={"memory_ids": [m["memory_id"]], "undo": True})
    assert len(client.get("/v1/review/queue").json()["items"]) == 1


def test_queue_handles_seed_style_memories(client):
    seed = propose(client, "手工记录的偏好")
    [item] = client.get("/v1/review/queue").json()["items"]
    assert item["review"]["run"] is None and item["review"]["priority"] == seed["confidence"]


def test_sensitive_pending_only_in_sensitive_view(app, client):
    m = candidate(app, seed_messages(client, ["我的投资计划"])[0], sensitivity="sensitive")
    data = client.get("/v1/review/queue").json()
    assert data["items"] == [] and data["counts"]["sensitive"] == 1
    sensitive = client.get("/v1/review/queue", params={"view": "sensitive"}).json()["items"]
    assert [i["memory_id"] for i in sensitive] == [m["memory_id"]]


def test_queue_before_tables_exist(app, client):
    with app.state.store.read() as db:
        assert not initialized(db)
    counts = client.get("/v1/review/queue").json()["counts"]
    assert counts == {"active": 0, "deferred": 0, "low_priority": 0, "sensitive": 0}


def test_queue_is_scoped_and_web_only(app, client):
    m = candidate(app, seed_messages(client, ["别的项目里的偏好"], project_id="other")[0], project_id="other")
    assert client.get("/v1/review/queue").json()["items"] == []
    other = client.get("/v1/review/queue", params={"project_id": "other"}).json()["items"]
    assert [i["memory_id"] for i in other] == [m["memory_id"]]
    headers = {"Authorization": "Bearer " + client_token(client)}
    assert client.get("/v1/review/queue", headers=headers).status_code == 403
    assert client.post("/v1/review/defer", json={"memory_ids": ["x"]}, headers=headers).status_code == 403


def test_review_tables_survive_backup_restore(app, client):
    m = candidate(app, seed_messages(client, ["我喜欢安静的咖啡馆"])[0])
    client.post("/v1/review/defer", json={"memory_ids": [m["memory_id"]]})
    backup = client.post("/v1/backup").json()
    data = client.get(backup["download_url"]).content
    restored = client.post("/v1/restore", files={"file": ("backup.zip", data)}).json()
    with Store(Path(restored["restored_home"])).read() as db:
        assert db.execute("SELECT count(*) FROM review_marks").fetchone()[0] == 1
