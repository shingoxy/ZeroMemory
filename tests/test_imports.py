import io
import json
import zipfile

import pytest

from zero_memory.core.errors import ServiceError
from zero_memory.importers.chatgpt import records
from zero_memory.schemas.models import Proposal, Search

from .conftest import import_data


def branching():
    return [
        {
            "id": "synthetic-branch",
            "title": "合成星舟项目",
            "current_node": "b2",
            "mapping": {
                "root": {"parent": None, "children": ["u1"], "message": None},
                "u1": {
                    "parent": "root",
                    "children": ["a1", "a2"],
                    "message": {
                        "id": "msg1",
                        "author": {"role": "user"},
                        "create_time": 1750000000,
                        "content": {"content_type": "text", "parts": ["我正在开发星舟项目。"]},
                    },
                },
                "a1": {
                    "parent": "u1",
                    "children": [],
                    "message": {
                        "id": "msg2",
                        "author": {"role": "assistant"},
                        "content": {"parts": ["我猜用户喜欢红色。"]},
                    },
                },
                "a2": {
                    "parent": "u1",
                    "children": ["b2"],
                    "message": {
                        "id": "msg3",
                        "author": {"role": "assistant"},
                        "content": {"parts": ["另一条分支。"]},
                    },
                },
                "b2": {
                    "parent": "a2",
                    "message": {
                        "id": "tool1",
                        "author": {"role": "tool"},
                        "content": {"content_type": "execution_output", "text": "合成工具输出"},
                    },
                },
                "empty": {
                    "parent": "a1",
                    "message": {"id": "empty", "author": {"role": "user"}, "content": {"parts": []}},
                },
                "rich": {
                    "parent": "a1",
                    "message": {
                        "id": "rich",
                        "author": {"role": "user"},
                        "content": {
                            "parts": [
                                "看这个",
                                {
                                    "content_type": "image_asset_pointer",
                                    "asset_pointer": "file-service://synthetic",
                                },
                            ]
                        },
                    },
                },
            },
        }
    ]


def test_branch_rich_tool_empty_and_missing_time(client):
    job = import_data(client, branching())
    assert job["status"] == "completed" and job["inserted"] == 6
    messages = client.get("/v1/messages").json()["items"]
    assert {m["node_id"] for m in messages} == {"u1", "a1", "a2", "b2", "empty", "rich"}


def test_branches_not_concatenated(client):
    import_data(client, branching())
    messages = client.get("/v1/messages").json()["items"]
    by_id = {m["node_id"]: m for m in messages}
    assert by_id["a1"]["parent_id"] == by_id["a2"]["parent_id"] == "u1"
    assert by_id["b2"]["parent_id"] == "a2"
    assert by_id["empty"]["content"] == "" and by_id["empty"]["timestamp"] is None
    assert "image_asset_pointer" in by_id["rich"]["content"]


def test_assistant_inference_not_user_fact(client, app, scope):
    import_data(client, branching())
    messages = client.get("/v1/messages").json()["items"]
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert client.post("/v1/extractions/quote/" + assistant["key"], json={}).status_code == 400
    body = dict(
        content="用户喜欢红色",
        evidence=assistant["content"],
        source_type="chatgpt_export",
        source_conversation_id=assistant["conversation_id"],
        source_message_keys=[assistant["key"]],
        source_message_ids=[assistant["message_id"]],
        source_role="assistant",
    )
    assert client.post("/v1/memories/propose", json=body).status_code == 400
    body["inference_type"] = "assistant_inference"
    memory = client.post("/v1/memories/propose", json=body).json()
    assert memory["status"] == "pending_review" and app.state.backend.search(Search(query="红色")) == []


def test_manual_quote_evidence_and_no_fake_llm(client, app):
    import_data(client, branching())
    message = next(m for m in client.get("/v1/messages").json()["items"] if m["message_id"] == "msg1")
    memory = client.post("/v1/extractions/quote/" + message["key"], json={}).json()
    assert memory["evidence_verified"] and memory["content"] == message["content"]
    assert client.post("/v1/memories/" + memory["memory_id"] + "/approve", json={}).status_code == 200
    assert client.post("/v1/extractions/run", json={}).status_code == 503
    assert app.state.models.get().provider == "none"


def test_evidence_must_match_exact_source(client, app, scope):
    import_data(client, [{"role": "user", "content": "合成原文", "message_id": "u"}])
    m = client.get("/v1/messages").json()["items"][0]
    with pytest.raises(ServiceError, match="原文片段"):
        app.state.backend.propose(
            Proposal(
                content="伪造",
                evidence="不在原文",
                source_type="generic_import",
                source_message_keys=[m["key"]],
                source_message_ids=["u"],
                source_conversation_id="generic",
            ),
            scope,
        )


def test_incremental_edited_message_preserves_both_versions(client):
    first = [{"role": "user", "content": "版本 A", "message_id": "same", "conversation_id": "c"}]
    assert import_data(client, first)["inserted"] == 1
    assert import_data(client, first)["inserted"] == 0
    first[0]["content"] = "版本 B"
    assert import_data(client, first)["inserted"] == 1
    messages = client.get("/v1/messages").json()["items"]
    assert len(messages) == 2 and len({m["key"] for m in messages}) == 2


def test_zip_split_json_and_wrapper(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "conversations-1.json",
            json.dumps(
                {"conversations": [{"id": "c1", "messages": [{"role": "user", "content": "第一卷"}]}]}
            ),
        )
        z.writestr(
            "conversations-2.json",
            json.dumps([{"id": "c2", "messages": [{"role": "user", "content": "第二卷"}]}]),
        )
        z.writestr("user.json", json.dumps({"ignored": "account metadata"}))
    job = import_data(client, buf.getvalue(), "export.zip")
    assert job["inserted"] == 2 and len(job["conversations"]) == 2


def test_official_zip_ignores_asset_index_and_shared_metadata(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("conversation_asset_file_names.json", json.dumps({"file_demo.dat": "example.png"}))
        z.writestr("shared_conversations.json", json.dumps([{"id": "share", "title": "share metadata"}]))
        z.writestr("sectioned_conversations.json", "[]")
        for number in range(2):
            z.writestr(f"archive/conversations-{number:03}.json", json.dumps([
                {"id": f"c{number}", "messages": [{"role": "user", "content": f"合成卷 {number}"}]}
            ]))
    first = import_data(client, buf.getvalue(), "official.zip")
    assert first["status"] == "completed" and first["inserted"] == 2 and first["failed"] == 0
    assert len(first["conversations"]) == 2
    repeated = import_data(client, buf.getvalue(), "renamed.zip")
    assert repeated["inserted"] == 0 and repeated["duplicates"] == 2


@pytest.mark.parametrize("name", ["../escape.json", "/absolute.json", "C:/file.json", "..\\escape.json"])
def test_zip_traversal(client, name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, "[]")
    response = client.post("/v1/imports", files={"file": ("bad.zip", buf.getvalue())})
    assert response.status_code == 400 and response.json()["error"]["code"] == "zip_path"


def test_zip_bomb_and_upload_limit(client, app):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("conversations.json", b"0" * 2_000_000)
    assert (
        client.post("/v1/imports", files={"file": ("bomb.zip", buf.getvalue())}).json()["error"]["code"]
        == "zip_limit"
    )
    app.state.importer.settings.max_upload_bytes = 100
    assert client.post("/v1/imports", files={"file": ("large.json", b" " * 101)}).status_code == 413


def test_zip_member_limit(tmp_path, app):
    source = tmp_path / "oversize.zip"
    with zipfile.ZipFile(source, "w") as z:
        z.writestr("conversations.json", b"[]" + b" " * 200)
    app.state.importer.settings.max_member_bytes = 100
    with pytest.raises(ServiceError):
        list(records(source, app.state.importer.settings))


@pytest.mark.parametrize(
    "data", [b'{"broken":', b'[{"role":"user"}, garbage]', b"not json", b"[null]", b'{"foo":[]}']
)
def test_malformed_inputs(client, data):
    response = client.post("/v1/imports", files={"file": ("bad.json", data)})
    assert response.status_code in {400, 422}


def test_cycle_rejected(client):
    data = branching()
    data[0]["mapping"]["u1"]["parent"] = "a1"
    response = client.post("/v1/imports", files={"file": ("cycle.json", json.dumps(data).encode())})
    assert response.json()["error"]["code"] == "mapping_cycle"


def test_selection_time_and_sensitive_extraction_skip(client):
    data = [
        {
            "role": "user",
            "content": "合成密码 password",
            "conversation_id": "a",
            "timestamp": "2026-01-01T00:00:00Z",
        },
        {"role": "user", "content": "不选择", "conversation_id": "b", "timestamp": "2026-01-01T00:00:00Z"},
    ]
    r = client.post("/v1/imports", files={"file": ("data.json", json.dumps(data).encode())}).json()
    client.post(
        f"/v1/imports/{r['id']}/start", json={"conversation_ids": ["a"], "since": "2025-01-01T00:00:00Z"}
    )
    assert len(client.get("/v1/messages").json()["items"]) == 1
    assert client.get("/v1/system").json()["counts"]["extractions"] == {"skipped_sensitive": 1}


def test_batch_failure_resume_atomic(client, app, monkeypatch, scope):
    records = [{"role": "user", "content": f"合成 {i}", "message_id": str(i)} for i in range(450)]
    job = client.post("/v1/imports", files={"file": ("batch.json", json.dumps(records).encode())}).json()
    original = app.state.importer._save

    def fail(db, value):
        if value["cursor"] >= 400 and value["status"] == "running":
            raise RuntimeError("synthetic fault")
        return original(db, value)

    monkeypatch.setattr(app.state.importer, "_save", fail)
    client.post(f"/v1/imports/{job['id']}/start", json={})
    failed = app.state.importer.get(job["id"], scope)
    assert failed["status"] == "failed" and failed["cursor"] == 200 and failed["inserted"] == 200
    monkeypatch.setattr(app.state.importer, "_save", original)
    client.post(f"/v1/imports/{job['id']}/start", json={})
    final = app.state.importer.get(job["id"], scope)
    assert final["status"] == "completed" and final["inserted"] == 450
