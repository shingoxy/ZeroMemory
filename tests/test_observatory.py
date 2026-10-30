import json
from uuid import uuid4

from fastapi.testclient import TestClient

from scripts.baseline_snapshot import fingerprint
from zero_memory.core.auth import Principal
from zero_memory.core.store import now
from zero_memory.observatory.service import ObservatoryQuery
from zero_memory.schemas.models import Memory, Proposal, Scope

from .conftest import approve, propose


def test_real_backend_mapping_pending_and_provenance(client):
    m = propose(client, "合成待审核记忆")
    view = client.get("/v1/observatory/snapshot").json()
    assert view["schema"] == "memory-observatory/v1" and view["demo"] is False
    assert view["count"] == 1 and view["memories"][0]["id"] == m["memory_id"]
    assert view["memories"][0]["status"] == "pending_review"
    assert "来源待核实" in view["memories"][0]["source_verification"]
    assert client.get("/v1/observatory/memories/" + m["memory_id"]).json()["evidence_verified"] is False


def test_observing_search_timeline_and_details_never_changes_data(client, app):
    m = propose(client, "合成星海只读记忆")
    before = fingerprint(app.state.store)
    for _ in range(3):
        assert client.get("/v1/observatory/snapshot?query=星海").json()["count"] == 1
        assert client.get("/v1/observatory/timeline").status_code == 200
        assert client.get("/v1/observatory/memories/" + m["memory_id"]).status_code == 200
        assert client.post("/v1/observatory/search", json={"query": "星海"}).status_code == 200
    assert fingerprint(app.state.store) == before


def test_scope_filter_before_graph_and_sensitive_hidden_by_default(client, app):
    safe = approve(client, propose(client, "合成可见", project_id="public"))
    approve(client, propose(client, "SECRET_OTHER_SCOPE", project_id="private"))
    sensitive = approve(client, propose(client, "SECRET_SENSITIVE", project_id="public", sensitivity="sensitive"))
    created = client.post("/v1/clients", json={"name": "read", "project_id": "public"}).json()
    with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": "Bearer " + created["token"]}) as scoped:
        view = scoped.get("/v1/observatory/snapshot").json()
        assert [m["id"] for m in view["memories"]] == [safe["memory_id"]]
        assert "SECRET" not in json.dumps(view)
        assert scoped.get("/v1/observatory/snapshot?project_id=private").status_code == 403
        assert scoped.get("/v1/observatory/snapshot?status=pending_review").status_code == 403
        assert scoped.get("/v1/observatory/snapshot?include_sensitive=true").status_code == 403
        assert scoped.get("/v1/observatory/memories/" + sensitive["memory_id"]).status_code == 404
    assert client.get("/v1/observatory/snapshot?project_id=public&include_sensitive=true").json()["count"] == 2


def test_bounded_pagination_and_stable_ids(app, client, scope, monkeypatch):
    with app.state.store.write() as db:
        for i in range(1000):
            app.state.backend._put(db, Memory(
                **Proposal(content=f"合成分页 {i}", topic="synthetic").model_dump(), **scope.model_dump(),
                memory_id=str(uuid4()), extracted_at=now(),
            ))
    calls = []
    original = app.state.backend.search

    def bounded(query):
        calls.append(query.limit)
        return original(query)

    monkeypatch.setattr(app.state.backend, "search", bounded)
    first = client.get("/v1/observatory/snapshot?limit=500").json()
    second = client.get(f"/v1/observatory/snapshot?limit=500&cursor={first['next_cursor']}").json()
    assert len(first["memories"]) == len(second["memories"]) == 500
    assert second["next_cursor"] is None
    assert not ({m["id"] for m in first["memories"]} & {m["id"] for m in second["memories"]})
    assert max(calls) <= 200 and len(first["edges"]) <= 2000
    assert client.get("/v1/observatory/snapshot?limit=501").status_code == 422


def test_edge_labels_keep_inferences_distinct_and_partial_dates_honest(client, app, scope):
    a = propose(client, "合成主题 A", topic="topic", event_time="2026-10-01T00:00:00Z")
    propose(client, "合成主题 B", topic="topic", event_time="2026-10-02T00:00:00Z")
    propose(client, "合成年份", event_time_original="2019", event_time_precision="year")
    view = client.get("/v1/observatory/snapshot").json()
    assert {e["kind"] for e in view["edges"]} == {"semantic_association", "time_near"}
    assert all(e["inferred"] for e in view["edges"])
    partial = next(m for m in view["memories"] if m["event_time_original"] == "2019")
    assert partial["event_time"] is None and partial["time_basis"] == "extracted_at_not_event"
    assert a["memory_id"] in {m["id"] for m in view["memories"]}


def test_unknown_scope_empty_is_not_demo(app):
    scope = Scope(project_id="empty")
    view = app.state.observatory.snapshot(ObservatoryQuery(**scope.model_dump()), Principal("admin", True, scope))
    assert view["memories"] == [] and view["demo"] is False
