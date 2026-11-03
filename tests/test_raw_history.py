import json
import zipfile
from pathlib import Path

import pytest

from zero_memory.core.backup import Backups
from zero_memory.core.config import Settings
from zero_memory.core.errors import ServiceError
from zero_memory.core.store import Store
from zero_memory.importers import raw_history
from zero_memory.importers.openai_preview import preview_archive, save_preview

from .conftest import import_data, propose
from .test_openai_preview import CANARY, node


def source(tmp_path, name="export", edited=False, multiple=False):
    path = tmp_path / (name + ".zip")
    with zipfile.ZipFile(path, "w") as archive:
        conversation = {"id": "c", "title": CANARY, "current_node": "reply", "mapping": {
            "system": node(None, ["u", "alternate"], "system", hidden=True),
            "u": node("system", ["reply"], "user", "  changed\t " if edited else "  original\t "),
            "alternate": node("system", [], "tool", {"content_type": "multimodal_text", "parts": [
                {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-demo"}]}),
            "reply": node("u", [], "assistant", {"content_type": "reasoning_recap", "content": "Exact recap"}),
        }}
        archive.writestr("conversations-000.json", json.dumps([conversation]))
        if multiple:
            archive.writestr("conversations-001.json", json.dumps([{**conversation, "id": "c2"}]))
        archive.writestr("file-demo.dat", b"test attachment")
    return path


def prepared(store, path):
    preview = preview_archive(path, store.path)
    save_preview(preview, store.home)
    return preview


def run(store, preview):
    return raw_history.run_confirmed(store, preview, f"确认导入 {preview['preview_id']}")


def test_archive_binding_precedes_schema_writes(tmp_path, app):
    store = app.state.store
    path = source(tmp_path)
    preview = prepared(store, path)
    with pytest.raises(ServiceError, match="需要匹配"):
        raw_history.run_confirmed(store, preview, "确认导入 wrong")
    with path.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ServiceError, match="ZIP 与预览不一致"):
        run(store, preview)
    with store.read() as db:
        assert not raw_history.initialized(db)


def test_existing_versions_are_reused_and_search_is_web_only(tmp_path, app, client, capsys):
    store = app.state.store
    path = source(tmp_path)
    assert import_data(client, path.read_bytes(), path.name)["inserted"] == 4
    with store.read() as db:
        before = [tuple(r) for r in db.execute("SELECT * FROM messages ORDER BY key")]
        old_queue = db.execute("SELECT count(*) FROM extraction_tasks").fetchone()[0]
    job = run(store, prepared(store, path))
    assert job["status"] == "completed" and job["messages"] == {"duplicates": 4, "metadata_added": 1, "unsupported": 1}
    with store.read() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM messages ORDER BY key")] == before
        assert db.execute("SELECT count(*) FROM extraction_tasks").fetchone()[0] == old_queue
    replay = run(store, prepared(store, path))
    assert replay["messages"] == {"duplicates": 4, "unsupported": 1}
    response = client.get("/v1/raw-history/conversations", params={"query": "Exact recap"})
    assert response.status_code == 200
    sid = response.json()["items"][0]["snapshot_id"]
    messages = f"/v1/raw-history/conversations/{sid}/messages"
    main = client.get(messages).json()["items"]
    assert [m["message_id"] for m in main] == ["u", "reply"]
    assert main[0]["content_text"] == "  original\t "
    assert main[0]["source_ref"]["json_pointer"] == "/0/mapping/u/message"
    assert len(client.get(messages, params={"show_hidden": True}).json()["items"]) == 3
    attachment = client.get(messages, params={"branch": "branch:alternate"}).json()["items"][0]["attachments"][0]
    assert attachment["member_name"] == "file-demo.dat" and attachment["size_bytes"] == 15
    token = client.post("/v1/clients", json={"name": "synthetic"}).json()["token"]
    for url in (messages, "/v1/raw-history/conversations", "/v1/raw-history/batches", "/v1/messages", "/v1/imports"):
        assert client.get(url, headers={"Authorization": "Bearer " + token}).status_code == 403
    assert client.get(messages, params={"project_id": "other"}).status_code == 404
    evidence = "/v1/raw-history/evidence/"
    assert client.get(evidence + main[1]["message_key"]).json() == {
        "snapshot_id": sid, "branch": "main", "show_hidden": False,
        "position": 1, "observed_at": main[1]["observed_at"],
    }
    alternate = client.get(messages, params={"branch": "branch:alternate"}).json()["items"][0]
    assert client.get(evidence + alternate["message_key"]).json()["branch"] == "branch:alternate"
    system = client.get(messages, params={"show_hidden": True}).json()["items"][0]
    assert client.get(evidence + system["message_key"]).json()["show_hidden"] is True
    assert client.get(evidence + main[1]["message_key"], params={"project_id": "other"}).status_code == 404
    assert client.get(evidence + main[1]["message_key"], headers={"Authorization": "Bearer " + token}).status_code == 403
    assert client.get(evidence + "missing").status_code == 404
    assert client.post("/v1/memories/search", json={"query": "Exact recap"}).json()["items"] == []
    assert CANARY not in json.dumps(job) + capsys.readouterr().out


def test_edit_versions_backup_and_confirmed_rollback(tmp_path, app, client):
    store = app.state.store
    first = run(store, prepared(store, source(tmp_path)))
    edited = run(store, prepared(store, source(tmp_path, "edited", edited=True)))
    assert first["messages"]["inserted"] == 4
    assert edited["messages"]["inserted"] == 1
    with store.read() as db:
        versions = [json.loads(r[0]) for r in db.execute("SELECT body FROM raw_messages WHERE node_id='u'")]
        assert {m["message_version"]: m["content_text"] for m in versions} == {1: "  original\t ", 2: "  changed\t "}
        assert db.execute("SELECT count(*) FROM extraction_tasks").fetchone()[0] == 0
    for version in versions:
        target = client.get("/v1/raw-history/evidence/" + version["message_key"]).json()
        page = client.get(f"/v1/raw-history/conversations/{target['snapshot_id']}/messages",
                          params={"offset": target["position"], "limit": 1}).json()
        assert page["items"][0]["content_text"] == version["content_text"]
    backups = Backups(store, Settings(_env_file=None, home=store.home))
    backup = backups.create()
    with (store.home / "backups" / backup["filename"]).open("rb") as stream:
        restored = Store(Path(backups.restore(stream)["restored_home"]))
    with restored.read() as db:
        assert db.execute("SELECT count(*) FROM raw_messages").fetchone()[0] == 8
        assert db.execute("SELECT count(*) FROM raw_history_fts WHERE raw_history_fts MATCH 'changed'").fetchone()[0] == 1
    plan = raw_history.rollback_batch(store, edited["id"])
    assert plan["counts"]["messages"] == 1 and plan["counts"]["index_rows"] == 4
    with pytest.raises(ServiceError):
        raw_history.rollback_batch(store, edited["id"], "wrong")
    assert not raw_history.rollback_batch(store, edited["id"], plan["confirmation"])["dry_run"]
    with store.read() as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM raw_messages").fetchone()[0] == 4


def test_interrupted_import_resumes_committed_conversations(tmp_path, app, monkeypatch):
    store = app.state.store
    preview = prepared(store, source(tmp_path, multiple=True))
    scan = raw_history._scan

    def interrupted(*args):
        yield next(scan(*args))
        raise OSError("synthetic interruption")

    monkeypatch.setattr(raw_history, "_scan", interrupted)
    failed = run(store, preview)
    assert failed["status"] == "interrupted" and failed["cursor"] == 1
    monkeypatch.setattr(raw_history, "_scan", scan)
    resumed = run(store, preview)
    assert resumed["status"] == "completed" and resumed["cursor"] == 2
    assert resumed["messages"]["inserted"] == 8
    assert run(store, preview) == resumed


def test_association_suggestions_are_scoped_read_only_and_exclude_sensitive(client, app):
    store = app.state.store
    old = propose(client, "original anchor")
    sensitive = propose(client, "original sensitive", sensitivity="sensitive")
    target = propose(client, "updated summary")
    with store.write() as db:
        for item in (old, sensitive, target):
            source_type = "chatgpt_export" if item == target else "chatgpt_conversation_summary"
            db.execute("UPDATE memories SET body=json_set(body,'$.source_type',?,'$.evidence',?) WHERE id=?",
                       (source_type, "used to prefer original", item["memory_id"]))
    with store.read() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM memories ORDER BY id")]
    url = "/v1/raw-history/associations/" + target["memory_id"]
    response = client.get(url).json()
    assert response["has_time_change_wording"]
    assert response["total_matches"] == 1
    assert response["items"][0]["memory"]["memory_id"] == old["memory_id"]
    assert client.get(url, params={"project_id": "other"}).status_code == 404
    token = client.post("/v1/clients", json={"name": "synthetic"}).json()["token"]
    assert client.get(url, headers={"Authorization": "Bearer " + token}).status_code == 403
    with store.read() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM memories ORDER BY id")] == before
