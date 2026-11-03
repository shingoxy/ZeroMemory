import json
import sqlite3
import zipfile

import pytest

from zero_memory.core.errors import ServiceError
from zero_memory.importers.openai_preview import PreviewLimits, file_sha256, preview_archive, save_preview

from .conftest import import_data

CANARY = "PREVIEW_PRIVATE_CANARY"


def node(parent, children, role, content=None, *, stamp=1, hidden=False):
    return {"parent": parent, "children": children, "message": {
        "author": {"role": role}, "create_time": stamp,
        "content": {"content_type": "text", "parts": [CANARY]} if content is None else content,
        "metadata": {"is_visually_hidden_from_conversation": hidden},
    }}


def archive(tmp_path, conversation, name="conversations-001.json"):
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, json.dumps([conversation]))
        z.writestr("user.json", CANARY)
    return path


def test_preview_branch_counts_and_skips_nonchat_bytes(tmp_path, monkeypatch, capsys):
    path = archive(tmp_path, {"id": "c", "title": CANARY, "current_node": "tool", "mapping": {
        "root": node(None, ["u", "edit"], "system", hidden=True),
        "u": node("root", ["old", "new"], "user"),
        "edit": node("root", [], "user", stamp=None),
        "old": node("u", [], "assistant"),
        "new": node("u", ["tool"], "assistant", {"content_type": "multimodal_text", "parts": [
            {"content_type": "image_asset_pointer", "asset_pointer": CANARY},
            {"content_type": "audio_asset_pointer", "asset_pointer": CANARY},
        ]}),
        "tool": node("new", [], "tool", {"content_type": "code", "text": CANARY}),
    }})
    open_member = zipfile.ZipFile.open

    def checked_open(self, name, *args, **kwargs):
        assert name == "conversations-001.json"
        return open_member(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", checked_open)
    preview = preview_archive(path)
    assert preview["roles"] == {"system": 1, "user": 2, "assistant": 2, "tool": 1}
    assert preview["branches"] == {"main": 4, "other": 2}
    assert preview["counts"]["hidden_messages"] == preview["system_messages"] == 1
    assert preview["unsupported_types"] == {"multimodal_text": 1}
    assert preview["anomalies"] == {"missing_message_time": 1}
    saved = save_preview(preview, tmp_path).read_text(encoding="utf-8")
    assert CANARY not in saved + capsys.readouterr().out
    assert json.loads(saved)["zip_sha256"] == file_sha256(path)


def test_preview_existing_history_is_read_only(tmp_path, client, app):
    conversation = {"id": "c", "title": CANARY, "current_node": "u", "mapping": {
        "u": node(None, [], "user"),
    }}
    path = archive(tmp_path, conversation)
    assert import_data(client, path.read_bytes(), path.name)["inserted"] == 1
    database = app.state.store.path
    before = file_sha256(database)
    preview = preview_archive(path, database)
    assert preview["existing_conversations"] == preview["counts"]["existing_exact_message_versions"] == 1
    assert preview["new_legacy_message_versions"] == 0
    assert file_sha256(database) == before


@pytest.mark.parametrize(("parent", "anomaly"), [("u", "parent_cycle"), ("absent", "missing_parent")])
def test_broken_parent_chain_is_not_main(tmp_path, parent, anomaly):
    path = archive(tmp_path, {"id": "c", "current_node": "u", "mapping": {"u": node(parent, [], "user")}})
    preview = preview_archive(path)
    assert preview["anomalies"][anomaly] == 1
    assert preview["branches"] == {"unknown": 1}


@pytest.mark.parametrize("case", ["path", "ratio", "malformed", "member_limit", "conversation_limit"])
def test_preview_rejects_invalid_or_oversized_archive(tmp_path, case):
    path = tmp_path / "export.zip"
    name = "../conversations.json" if case == "path" else "conversations.json"
    content = {"ratio": "0" * 2_000_000, "malformed": f'[{CANARY}'}.get(case, '[{"mapping":{}}]')
    limits = {"member_limit": PreviewLimits(max_member_bytes=1),
              "conversation_limit": PreviewLimits(max_conversation_bytes=1)}.get(case)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, content)
    with pytest.raises(ServiceError) as error:
        preview_archive(path, limits=limits)
    assert CANARY not in str(error.value)


def test_preview_unknown_schema_preserves_database(tmp_path):
    database = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version=999")
    before = file_sha256(database)
    with pytest.raises(ServiceError) as error:
        preview_archive(archive(tmp_path, {}), database)
    assert error.value.code == "schema_version"
    assert file_sha256(database) == before
