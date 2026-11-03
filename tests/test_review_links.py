from zero_memory.memory.sqlite import SQLiteBackend
from zero_memory.schemas.models import Scope

from .conftest import approve
from .review_support import NOTE_TEXTS, RUN_B, candidate, seed_messages


def test_suggest_classifies_evidence_and_conflict(app, client):
    msgs = seed_messages(client, NOTE_TEXTS)
    old = approve(client, candidate(app, msgs[0], run_id=RUN_B))
    a, c = candidate(app, msgs[1]), candidate(app, msgs[2])
    assert client.post("/v1/review/links/suggest", json={}).json() == {
        "candidates": 2, "created": 2, "evidence": 1, "conflict": 1}
    [la] = client.get("/v1/review/links", params={"memory_id": a["memory_id"]}).json()["items"]
    assert (la["kind"], la["target_memory_id"], la["status"], la["time_version"]) == (
        "evidence", old["memory_id"], "suggested", None)
    [lc] = client.get("/v1/review/links", params={"memory_id": c["memory_id"]}).json()["items"]
    assert lc["kind"] == "conflict" and lc["signals"]["time_change_wording"] is True
    assert lc["time_version"] == {"new_valid_from": msgs[2]["timestamp"], "old_valid_to": msgs[2]["timestamp"]}
    links = {i["memory_id"]: i["review"]["links"] for i in client.get("/v1/review/queue").json()["items"]}
    assert links == {a["memory_id"]: {"evidence": 1, "conflict": 0}, c["memory_id"]: {"evidence": 0, "conflict": 1}}


def test_suggest_skips_sensitive_other_scope_and_same_run(app, client):
    msgs = seed_messages(client, [NOTE_TEXTS[0], NOTE_TEXTS[1], NOTE_TEXTS[0] + "的模板"])
    candidate(app, msgs[0], sensitivity="sensitive")
    candidate(app, msgs[1])
    candidate(app, msgs[2])
    other = seed_messages(client, [NOTE_TEXTS[0]], project_id="other")
    candidate(app, other[0], run_id=RUN_B, project_id="other")
    assert client.post("/v1/review/links/suggest", json={}).json() == {
        "candidates": 2, "created": 0, "evidence": 0, "conflict": 0}


def test_suggest_is_idempotent_and_keeps_decisions(app, client):
    msgs = seed_messages(client, NOTE_TEXTS[:2])
    approve(client, candidate(app, msgs[0], run_id=RUN_B))
    a = candidate(app, msgs[1])
    client.post("/v1/review/links/suggest", json={})
    [link] = client.get("/v1/review/links", params={"memory_id": a["memory_id"]}).json()["items"]
    decided = client.post(f"/v1/review/links/{link['link_id']}", json={"decision": "dismiss"}).json()
    assert decided["status"] == "dismissed"
    assert client.post("/v1/review/links/suggest", json={}).json()["created"] == 0
    assert client.get("/v1/review/links", params={"memory_id": a["memory_id"]}).json()["items"][0]["status"] == "dismissed"


def test_confirm_evidence_link_does_not_modify_target(app, client):
    msgs = seed_messages(client, NOTE_TEXTS[:2])
    old = approve(client, candidate(app, msgs[0], run_id=RUN_B))
    a = candidate(app, msgs[1])
    client.post("/v1/review/links/suggest", json={})
    [link] = client.get("/v1/review/links", params={"memory_id": a["memory_id"]}).json()["items"]
    before = SQLiteBackend(app.state.store).get(old["memory_id"], Scope()).model_dump()
    decided = client.post(f"/v1/review/links/{link['link_id']}", json={"decision": "confirm"}).json()
    assert decided["status"] == "confirmed"
    assert SQLiteBackend(app.state.store).get(old["memory_id"], Scope()).model_dump() == before


def test_links_before_tables_exist(client):
    assert client.get("/v1/review/links", params={"memory_id": "missing"}).json() == {"items": []}
    assert client.post("/v1/review/links/suggest", json={}).json() == {
        "candidates": 0, "created": 0, "evidence": 0, "conflict": 0}
