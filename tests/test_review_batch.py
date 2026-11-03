from zero_memory.memory.sqlite import SQLiteBackend
from zero_memory.review import batch
from zero_memory.schemas.models import Scope

from .conftest import approve, propose
from .review_support import NOTE_TEXTS, RUN_B, candidate, seed_messages


def test_batch_needs_category_or_selection(client):
    assert client.post("/v1/review/batch/preview", json={"action": "approve"}).status_code == 422
    too_many = {"action": "reject", "memory_ids": ["x"] * 31}
    assert client.post("/v1/review/batch/preview", json=too_many).status_code == 422


def test_batch_approve_previews_samples_and_skips_ineligible(app, client):
    msgs = seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码", "我每周去游泳", "我在学日语", *NOTE_TEXTS[::2]])
    ok = [candidate(app, msgs[0]), candidate(app, msgs[1])]
    client.post("/v1/review/defer", json={"memory_ids": [candidate(app, msgs[2])["memory_id"]]})
    candidate(app, msgs[3], confidence=0.3)
    approve(client, candidate(app, msgs[4], run_id=RUN_B))
    candidate(app, msgs[5])
    client.post("/v1/review/links/suggest", json={})
    propose(client, "手工陈述", memory_type="user_profile")
    body = {"action": "approve", "memory_type": "user_profile"}
    preview = client.post("/v1/review/batch/preview", json=body).json()
    assert (preview["eligible"], preview["limit"]) == (2, 50)
    assert preview["skipped"] == {"deferred": 1, "low_priority": 1, "conflict_suggestion": 1, "unverified": 1}
    assert {s["memory_id"] for s in preview["samples"]} == {m["memory_id"] for m in ok}
    assert all(s["evidence"] for s in preview["samples"])
    assert preview["confirm"].startswith("确认批量批准 user_profile ")
    bad = {**body, "confirm": "确认批量批准 user_profile 000000000000"}
    assert client.post("/v1/review/batch/apply", json=bad).status_code == 409
    done = client.post("/v1/review/batch/apply", json={**body, "confirm": preview["confirm"]}).json()
    assert done == {"applied": 2, "errors": []}


def test_batch_apply_rejects_changed_set(app, client):
    msgs = seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码"])
    candidate(app, msgs[0])
    body = {"action": "reject", "memory_type": "user_profile"}
    preview = client.post("/v1/review/batch/preview", json=body).json()
    candidate(app, msgs[1])
    assert client.post("/v1/review/batch/apply", json={**body, "confirm": preview["confirm"]}).status_code == 409


def test_batch_apply_twice_is_harmless(app, client):
    m = candidate(app, seed_messages(client, ["我喜欢纸质笔记"])[0])
    body = {"action": "approve", "memory_ids": [m["memory_id"]]}
    preview = client.post("/v1/review/batch/preview", json=body).json()
    assert preview["confirm"].startswith("确认批量批准 所选 ")
    assert client.post("/v1/review/batch/apply", json={**body, "confirm": preview["confirm"]}).json()["applied"] == 1
    assert client.post("/v1/review/batch/apply", json={**body, "confirm": preview["confirm"]}).status_code == 409
    assert SQLiteBackend(app.state.store).get(m["memory_id"], Scope()).version == m["version"] + 1


def test_batch_limit_takes_highest_priority(app, client, monkeypatch):
    monkeypatch.setattr(batch, "APPROVE_LIMIT", 1)
    msgs = seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码"])
    candidate(app, msgs[0], confidence=0.7)
    top = candidate(app, msgs[1], confidence=0.9)
    preview = client.post("/v1/review/batch/preview", json={"action": "approve", "memory_type": "user_profile"}).json()
    assert (preview["matched"], preview["eligible"], [s["memory_id"] for s in preview["samples"]]) == (
        2, 1, [top["memory_id"]])


def test_batch_defer_by_category(app, client):
    for message in seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码"]):
        candidate(app, message)
    body = {"action": "defer", "memory_type": "user_profile"}
    preview = client.post("/v1/review/batch/preview", json=body).json()
    assert client.post("/v1/review/batch/apply", json={**body, "confirm": preview["confirm"]}).json()["applied"] == 2
    assert client.get("/v1/review/queue").json()["counts"]["deferred"] == 2


def test_batch_reject_skips_memory_approved_mid_apply(app, client, monkeypatch):
    from .test_review_revoke import _approve_other_on_first_reject

    first, second = (candidate(app, m) for m in seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码"]))
    body = {"action": "reject", "memory_type": "user_profile"}
    preview = client.post("/v1/review/batch/preview", json=body).json()
    _approve_other_on_first_reject(app, monkeypatch, first, second)
    done = client.post("/v1/review/batch/apply", json={**body, "confirm": preview["confirm"]}).json()
    statuses = sorted(SQLiteBackend(app.state.store).get(m["memory_id"], Scope()).status for m in (first, second))
    assert done["applied"] == 1 and len(done["errors"]) == 1 and statuses == ["approved", "rejected"]


def test_batch_reject_preview_reports_included_low_priority(app, client):
    msgs = seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码"])
    candidate(app, msgs[0], confidence=0.9)
    candidate(app, msgs[1], confidence=0.3)
    preview = client.post("/v1/review/batch/preview", json={"action": "reject", "memory_type": "user_profile"}).json()
    assert (preview["eligible"], preview["low_priority_included"]) == (2, 1)
