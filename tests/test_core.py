from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zero_memory.api.app import create_app
from zero_memory.core.config import Settings
from zero_memory.schemas.models import IdentityPatch, Search

from .conftest import approve, propose


def test_health_and_avatar(client):
    health = client.get("/health").json()
    assert health["memory"]["ready"] and not health["llm"]["ready"]
    assert client.get("/static/public/zero-avatar.png").headers["content-type"] == "image/png"


def test_identity_requires_explicit_authority(client):
    identity = client.get("/v1/identity").json()
    body = {k: identity[k] for k in ("soul", "style")}
    body["expected_version"] = 1
    assert client.patch("/v1/identity", json=body).status_code == 403
    body["authorized"] = True
    body["soul"]["traits"]["humor"] = 77
    assert client.patch("/v1/identity", json=body).json()["version"] == 2
    assert client.patch("/v1/identity", json=body).status_code == 409
    restored = client.post(
        "/v1/identity/rollback", json={"version": 1, "expected_version": 2, "authorized": True}
    ).json()
    assert restored["version"] == 3 and restored["soul"]["traits"]["humor"] == 55


@pytest.mark.parametrize("field,value", [("curiosity", -1), ("patience", 101), ("humor", "bad")])
def test_persona_bounds(client, field, value):
    identity = client.get("/v1/identity").json()
    identity["soul"]["traits"][field] = value
    assert (
        client.patch(
            "/v1/identity",
            json={
                "soul": identity["soul"],
                "style": identity["style"],
                "authorized": True,
                "expected_version": 1,
            },
        ).status_code
        == 422
    )


def test_state_deterministic_and_bounded(app):
    identity = app.state.identity
    for _ in range(100):
        state = identity.advance("engaged")
    assert state["patience_current"] == 100 and state["engagement"] == 100
    for _ in range(200):
        state = identity.advance("routine")
    assert state["patience_current"] == 0 and state["engagement"] == 0
    assert identity.advance("reset")["patience_current"] == 65


def test_restart_persists(client, app):
    memory = approve(client, propose(client))
    identity = app.state.identity.get()
    identity["soul"]["traits"]["curiosity"] = 91
    app.state.identity.update(
        IdentityPatch(authorized=True, expected_version=1, soul=identity["soul"], style=identity["style"])
    )
    second = create_app(Settings(_env_file=None, home=app.state.store.home))
    assert second.state.identity.get()["soul"]["traits"]["curiosity"] == 91
    result = second.state.backend.search(Search(query="星舟"))
    assert result[0]["memory_id"] == memory["memory_id"]
    assert (app.state.store.home / "identity/soul.yaml").exists()


@pytest.mark.parametrize("query", ["星舟", "正在开发星舟", "项目", "我正在开发星舟项目"])
def test_keyword_search_unicode(client, query):
    memory = approve(client, propose(client))
    result = client.post("/v1/memories/search", json={"query": query}).json()["items"]
    assert [x["memory_id"] for x in result] == [memory["memory_id"]]


def test_literal_query_no_wildcards_or_sql(client):
    approve(client, propose(client, "literal_100% and SELECT --"))
    assert len(client.post("/v1/memories/search", json={"query": "100%"}).json()["items"]) == 1
    assert client.post("/v1/memories/search", json={"query": "' OR 1=1 --"}).json()["items"] == []


def test_dedup_and_review(client):
    first = propose(client)
    assert propose(client)["memory_id"] == first["memory_id"]
    assert client.post("/v1/memories/search", json={}).json()["items"] == []
    assert client.post(f"/v1/memories/{first['memory_id']}/approve", json={}).status_code == 409
    approve(client, first)
    assert len(client.post("/v1/memories/search", json={}).json()["items"]) == 1


def test_edit_undo_delete_clears_index_and_revisions(client, app, scope):
    first = approve(client, propose(client))
    edited = client.patch(
        f"/v1/memories/{first['memory_id']}",
        json={"content": "我在开发月港项目", "expected_version": first["version"]},
    ).json()
    assert edited["status"] == "pending_review"
    assert app.state.backend.search(Search(query="星舟")) == []
    undo = client.post(f"/v1/memories/{first['memory_id']}/undo", json={"version": 1}).json()
    assert undo["content"] == first["content"] and undo["status"] == "pending_review"
    approve(client, undo)
    assert client.request("DELETE", f"/v1/memories/{first['memory_id']}", json={}).json()["deleted"]
    assert app.state.backend.search(Search(query="星舟")) == []
    with app.state.store.read() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0


def test_temporal_conflict(client):
    old = approve(
        client, propose(client, "我使用 2025 设备 A", event_time="2025-01-01T00:00:00Z", fact_key="device")
    )
    new = propose(client, "我在 2026 换成设备 B", event_time="2026-01-01T00:00:00Z", fact_key="device")
    assert new["conflicts"] == [old["memory_id"]]
    assert (
        client.post(f"/v1/memories/{new['memory_id']}/approve", json={"confirm_external": True}).status_code
        == 409
    )
    new = approve(client, new, supersedes=old["memory_id"])
    history = client.post("/v1/memories/search", json={"status": "superseded"}).json()["items"]
    assert history[0]["memory_id"] == old["memory_id"] and history[0]["valid_to"] == new["event_time"]
    earlier = propose(client, "早期设备 C", fact_key="device", event_time="2024-01-01T00:00:00Z")
    assert (
        client.post(
            f"/v1/memories/{earlier['memory_id']}/approve",
            json={"confirm_external": True, "supersedes": new["memory_id"]},
        ).status_code
        == 409
    )


def test_conflicts_detected_at_approval_not_only_proposal(client):
    a = propose(client, "设备 A", fact_key="device")
    b = propose(client, "设备 B", fact_key="device")
    approve(client, a)
    assert (
        client.post(f"/v1/memories/{b['memory_id']}/approve", json={"confirm_external": True}).status_code
        == 409
    )


def test_similar_merge_preserves_each_source_and_requires_review(client):
    a = approve(client, propose(client, "我正在开发星舟项目，使用本地记忆。"))
    b = propose(client, "我正在开发星舟项目，使用本地记忆和审核。")
    merged = client.post(
        "/v1/memories/merge",
        json={"memory_ids": [a["memory_id"], b["memory_id"]], "content": "星舟项目采用本地记忆与人工审核。"},
    ).json()
    assert merged["status"] == "pending_review" and len(merged["merged_evidence"]) == 2
    assert merged["merged_evidence"][0]["content"] == a["content"]
    approve(client, merged)
    active = client.post("/v1/memories/search", json={}).json()["items"]
    assert [m["memory_id"] for m in active] == [merged["memory_id"]]


def test_expired_memory_not_in_current_context(client):
    approve(client, propose(client, "已经过期的星舟状态", valid_to="2024-01-01T00:00:00Z"))
    assert client.post("/v1/memories/search", json={}).json()["items"] == []


def test_revoke_reject(client):
    memory = approve(client, propose(client))
    assert client.post(f"/v1/memories/{memory['memory_id']}/revoke", json={}).status_code == 200
    assert client.post("/v1/memories/search", json={}).json()["items"] == []
    new = propose(client, "另一个候选")
    assert client.post(f"/v1/memories/{new['memory_id']}/reject", json={}).json()["status"] == "rejected"


@pytest.mark.parametrize("config", [{"host": "0.0.0.0"}, {"memory_backend": "unknown"}, {"port": 0}])
def test_bad_config_fails(config):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **config)


def test_everos_unavailable_fallback(tmp_path):
    app = create_app(
        Settings(_env_file=None, home=tmp_path, memory_backend="everos", everos_base_url="http://127.0.0.1:1")
    )
    with TestClient(app, base_url="http://127.0.0.1") as c:
        health = c.get("/health").json()
        assert health["memory"]["degraded"] and not health["everos"]["reachable"]


def test_disk_directory_failure(tmp_path):
    file = tmp_path / "not-a-directory"
    file.write_text("user file", encoding="utf-8")
    with pytest.raises(OSError):
        create_app(Settings(_env_file=None, home=Path(file)))
