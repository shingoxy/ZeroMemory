from zero_memory.memory.sqlite import SQLiteBackend
from zero_memory.schemas.models import Scope

from .conftest import approve
from .review_support import RUN_A, RUN_B, candidate, seed_messages


def test_run_revoke_dry_run_then_rejects_only_pending(app, client):
    msgs = seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码", "我每周去游泳", "我在学日语"])
    p1, p2 = candidate(app, msgs[0]), candidate(app, msgs[1])
    kept = approve(client, candidate(app, msgs[2]))
    other = candidate(app, msgs[3], run_id=RUN_B)
    url = f"/v1/review/runs/{RUN_A}/revoke"
    dry = client.post(url, json={}).json()
    assert dry == {"run_id": RUN_A, "pending": 2, "approved": 1, "other": 0, "sensitive_items": 0,
                   "confirm": f"确认撤销批次 {RUN_A}"}
    assert client.post(url, json={"confirm": "确认撤销批次 x"}).status_code == 409
    assert client.post(url, json={"confirm": dry["confirm"]}).json() == {
        "run_id": RUN_A, "rejected": 2, "sensitive_rejected": 0}
    backend = SQLiteBackend(app.state.store)
    assert [backend.get(m["memory_id"], Scope()).status for m in (p1, p2, kept, other)] == [
        "rejected", "rejected", "approved", "pending_review"]
    with app.state.store.read() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE action='memory_run_revoke'").fetchone()[0] == 2


def test_run_revoke_unknown_run(app, client):
    assert client.post("/v1/review/runs/" + "c" * 64 + "/revoke", json={}).status_code == 404
    assert client.post("/v1/review/runs/not-a-run/revoke", json={}).status_code == 422
    with app.state.store.read() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE action='memory_run_revoke'").fetchone()[0] == 0
