import json

import pytest

from zero_memory.memory.sqlite import SQLiteBackend
from zero_memory.schemas.models import Scope

from .conftest import approve, propose
from .review_support import RUN_A, candidate, client_token, seed_messages

SECRET = "ZM-CANARY-5521"
FOLDER = "review_queue/v03b/sensitive"


def write_item(app, message, *, run_id=RUN_A, item_id="c" * 64, content=f"合成敏感候选 {SECRET}"):
    # 字段与 cloud_batch.save_candidate 写入的敏感文件一致
    folder = app.state.store.home / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    item = {"status": "pending_review", "run_id": run_id, "source_message_key": message["key"],
            "source_message_id": message["message_id"], "source_conversation_id": message["conversation_id"],
            "observed_at": message["timestamp"], "event_date": None, "sensitivity": "sensitive",
            "candidate": {"content": content, "evidence": message["text"], "memory_type": "user_profile",
                          "confidence": 0.6, "fact_key": None, "sensitivity": "sensitive", "summary_links": [],
                          "claim_kind": "user_reported"}}
    (folder / f"{item_id}.json").write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")


def test_client_search_and_context_exclude_sensitive(client):
    m = approve(client, propose(client, f"合成敏感事实 {SECRET}", sensitivity="sensitive"))
    headers = {"Authorization": "Bearer " + client_token(client)}
    assert client.post("/v1/memories/search", json={"query": "合成敏感事实"}, headers=headers).json()["items"] == []
    assert SECRET not in client.get("/v1/context", params={"query": "合成敏感事实"}, headers=headers).text
    admin = client.post("/v1/memories/search", json={"query": "合成敏感事实"}).json()["items"]
    assert [x["memory_id"] for x in admin] == [m["memory_id"]]


def test_sensitive_listing_is_scoped_with_rule_hits(app, client):
    write_item(app, seed_messages(client, ["我的投资计划是每月定投"])[0])
    write_item(app, seed_messages(client, ["我的投资计划是每月定投"], project_id="other")[0], item_id="d" * 64)
    data = client.get("/v1/review/sensitive").json()
    assert [i["item_id"] for i in data["items"]] == ["c" * 64] and data["items"][0]["rule_hits"] == ["投资"]


def test_sensitive_listing_skips_malformed_files(app, client):
    folder = app.state.store.home / FOLDER
    folder.mkdir(parents=True)
    (folder / ("e" * 64 + ".json")).write_text("{broken", encoding="utf-8")
    assert client.get("/v1/review/sensitive").json() == {"items": [], "skipped_files": 1}


def test_item_id_must_be_hex(client):
    assert client.post("/v1/review/sensitive/" + "z" * 64, json={"action": "reject"}).status_code == 422


def test_keep_sensitive_approves_and_hides_from_clients(app, client):
    write_item(app, seed_messages(client, ["我的投资计划是每月定投"])[0], content="每月定投")
    result = client.post("/v1/review/sensitive/" + "c" * 64, json={"action": "keep_sensitive"}).json()
    assert (result["status"], result["approved"]) == ("kept_sensitive", True)
    memory = SQLiteBackend(app.state.store).get(result["memory_id"], Scope())
    assert (memory.sensitivity, memory.status) == ("sensitive", "approved")
    headers = {"Authorization": "Bearer " + client_token(client)}
    assert client.post("/v1/memories/search", json={"query": "定投"}, headers=headers).json()["items"] == []
    assert client.get("/v1/review/sensitive").json()["items"] == []


def test_to_private_moves_into_regular_queue(app, client):
    write_item(app, seed_messages(client, ["我的投资计划是每月定投"])[0], content="每月定投")
    result = client.post("/v1/review/sensitive/" + "c" * 64, json={"action": "to_private"}).json()
    [item] = client.get("/v1/review/queue").json()["items"]
    assert (result["status"], item["memory_id"], item["sensitivity"]) == ("moved_to_review", result["memory_id"], "private")


def test_reject_strips_candidate_text(app, client):
    write_item(app, seed_messages(client, ["我的投资计划是每月定投"])[0])
    client.post("/v1/review/sensitive/" + "c" * 64, json={"action": "reject"})
    text = (app.state.store.home / FOLDER / ("c" * 64 + ".json")).read_text(encoding="utf-8")
    assert SECRET not in text and '"candidate_sha256"' in text and '"rejected"' in text


def test_run_revoke_rejects_sensitive_items_of_run(app, client):
    write_item(app, seed_messages(client, ["我的投资计划是每月定投"])[0])
    url = f"/v1/review/runs/{RUN_A}/revoke"
    assert client.post(url, json={}).json()["sensitive_items"] == 1
    assert client.post(url, json={"confirm": f"确认撤销批次 {RUN_A}"}).json()["sensitive_rejected"] == 1


def test_db_sensitive_pending_is_excluded_from_client_context(app, client):
    candidate(app, seed_messages(client, [f"我的投资账户 {SECRET}"])[0], sensitivity="sensitive")
    headers = {"Authorization": "Bearer " + client_token(client)}
    assert SECRET not in client.get("/v1/context", params={"query": "投资账户"}, headers=headers).text


REVIEW_ROUTES = [("GET", "/v1/review/queue"), ("POST", "/v1/review/defer"), ("POST", "/v1/review/batch/preview"),
                 ("POST", "/v1/review/batch/apply"), ("GET", "/v1/review/runs"),
                 ("POST", f"/v1/review/runs/{RUN_A}/revoke"), ("POST", "/v1/review/links/suggest"),
                 ("GET", "/v1/review/links"), ("POST", "/v1/review/links/" + "a" * 64),
                 ("GET", "/v1/review/sensitive"), ("POST", "/v1/review/sensitive/" + "c" * 64)]


@pytest.mark.parametrize(("method", "url"), REVIEW_ROUTES)
def test_all_review_routes_reject_client_tokens(client, method, url):
    headers = {"Authorization": "Bearer " + client_token(client)}
    assert client.request(method, url, json={}, headers=headers).status_code == 403
