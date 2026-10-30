import json
import subprocess
import sys
import zipfile
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from zero_memory.core.backup import Backups
from zero_memory.core.errors import ServiceError
from zero_memory.core.identity import Identity
from zero_memory.core.store import Store, digest, dumps
from zero_memory.importers.migration import MEMBERS, PREFIX, SEED, import_bundle
from zero_memory.memory.sqlite import SQLiteBackend
from zero_memory.schemas.models import IdentityPatch, ModelConfig, Proposal, Scope, Search

from .conftest import import_data


def seed(**changes):
    return {
        "memory_id": str(uuid4()),
        **Scope().model_dump(),
        "memory_type": "user_profile",
        "content": "我目前使用合成相机 A",
        "source_type": "chatgpt_conversation_summary",
        "source_conversation_id": None,
        "source_message_ids": [],
        "source_role": "user",
        "evidence": "这是摘要转述，并非逐字引文",
        "event_time": "2026-01-01",
        "extracted_at": "2026-10-29T00:00:00+08:00",
        "status": "pending_review",
        "sensitivity": "low",
        "version": 1,
        "claim_kind": "user_reported",
        "origin_quality": "summary_only",
        "topic": "synthetic_camera",
        "review_note": "须原文核对",
        **changes,
    }


def bundle(tmp_path, memories, *, identity=None, name="general.zip"):
    payload = {n: b"synthetic fixture" for n in MEMBERS - {"manifest.json"}}
    payload[SEED] = "".join(dumps(m) + "\n" for m in memories).encode()
    payload["zero_identity.json"] = dumps(
        identity
        or {
            "agent_id": "zero-001",
            "name": "不得覆盖的新名字",
            "identity_start_date": "2026-10-28",
            "persona": {"user_confirmed_fields": ["name"], "proposed_traits_for_review": ["待审核建议"]},
        }
    ).encode()
    manifest = {
        "bundle": "ZeroMemory_Chat_Migration_Kit",
        "records_general": len(memories),
        "files": {n: {"bytes": len(v), "sha256": digest(v)} for n, v in payload.items()},
    }
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.comment = name.encode()
        for n, v in payload.items():
            z.writestr(PREFIX + n, v)
        z.writestr(PREFIX + "manifest.json", dumps(manifest))
    return p


def migrate(app, path):
    return import_bundle(path, app.state.store, app.state.importer.settings)


def test_seed_import_idempotent_by_uuid_dedup_and_repacked_zip(app, tmp_path, scope):
    raw = seed(dedup_key="synthetic-stable-key")
    first = migrate(app, bundle(tmp_path, [raw]))
    second = migrate(app, bundle(tmp_path, [raw], name="repacked.zip"))
    renamed_id = {**raw, "memory_id": str(uuid4())}
    third = migrate(app, bundle(tmp_path, [renamed_id], name="alias.zip"))
    assert first["inserted"] == 1 and first["failed"] == 0
    assert first["bundle_hash"] != second["bundle_hash"]
    assert second["duplicates"] == third["duplicates"] == 1
    assert second["inserted"] == third["inserted"] == 0
    assert first["identity_version"] == second["identity_version"] == third["identity_version"]
    memory = app.state.backend.get(raw["memory_id"], scope)
    assert memory.status == "pending_review" and memory.origin_quality == "summary_only"
    assert not memory.evidence_verified and memory.sensitivity == "private"
    assert memory.event_time == "2026-01-01T00:00:00+00:00"
    artifact = memory.source_artifact
    original = (app.state.store.home / "sources" / artifact.source_file).read_bytes()
    assert digest(original) == artifact.source_hash and json.loads(original)["memory_id"] == raw["memory_id"]
    assert app.state.backend.search(Search()) == []


def test_duplicate_import_does_not_reset_human_review(app, tmp_path, scope):
    raw = seed()
    p = bundle(tmp_path, [raw])
    migrate(app, p)
    app.state.backend.transition(raw["memory_id"], scope, "rejected")
    assert migrate(app, p)["duplicates"] == 1
    assert app.state.backend.get(raw["memory_id"], scope).status == "rejected"


def test_identity_merge_preserves_confirmed_fields_and_proposals_stay_inactive(app, tmp_path):
    old = app.state.identity.get()
    soul = deepcopy(old["soul"])
    soul["name"], soul["traits"]["humor"] = "用户确认的名字", 17
    app.state.identity.update(
        IdentityPatch(
            authorized=True,
            expected_version=old["version"],
            soul=soul,
            style=old["style"],
        )
    )
    result = migrate(app, bundle(tmp_path, [seed(claim_kind="assistant_proposal", source_role="mixed")]))
    current = app.state.identity.get()
    assert current["soul"] == soul and current["style"] == old["style"]
    assert current["migration"]["persona_proposals_pending_review"] == ["待审核建议"]
    assert result["identity_version"] == 3
    app.state.identity.update(
        IdentityPatch(
            authorized=True,
            expected_version=3,
            soul=soul,
            style=old["style"],
        )
    )
    assert app.state.identity.get()["migration"] == current["migration"]


def test_summary_requires_explicit_external_confirmation(client, app, tmp_path):
    raw = seed(claim_kind="assistant_proposal", source_role="mixed")
    migrate(app, bundle(tmp_path, [raw]))
    memory = app.state.backend.get(raw["memory_id"], Scope())
    assert memory.inference_type == "assistant_inference"
    assert client.post(f"/v1/memories/{raw['memory_id']}/approve", json={}).status_code == 409
    assert app.state.backend.search(Search()) == []


def test_changed_seed_retains_original_and_reports_review(app, tmp_path, scope):
    raw = seed(dedup_key="fixed-key")
    migrate(app, bundle(tmp_path, [raw]))
    changed = {**raw, "content": "改动的合成摘要"}
    report = migrate(app, bundle(tmp_path, [changed], name="changed.zip"))
    assert report["failed"] == 1 and report["errors"][0]["code"] == "seed_changed"
    assert app.state.backend.get(raw["memory_id"], scope).content == raw["content"]
    assert (app.state.store.home / "sources" / (report["bundle_hash"] + ".zip")).exists()


def test_sensitive_package_never_opened_and_sensitive_seed_skipped(app, tmp_path, monkeypatch):
    private = tmp_path / "ZeroMemory_Private_Review.zip"
    private.write_bytes(b"do not read")
    original = Path.open

    def guarded(path, *a, **kw):
        if path == private:
            raise AssertionError("private package was opened")
        return original(path, *a, **kw)

    monkeypatch.setattr(Path, "open", guarded)
    report = migrate(app, bundle(tmp_path, [seed(), seed(sensitivity="sensitive")]))
    assert report["inserted"] == report["skipped_sensitive"] == 1
    assert report["private_bundle_read"] is False


def test_bundle_integrity_before_mutation(app, tmp_path):
    p = bundle(tmp_path, [seed()])
    rewritten = tmp_path / "broken.zip"
    with zipfile.ZipFile(p) as source, zipfile.ZipFile(rewritten, "w") as target:
        for name in source.namelist():
            target.writestr(name, b"changed" if name.endswith(SEED) else source.read(name))
    with pytest.raises(ServiceError, match="校验失败"):
        migrate(app, rewritten)
    assert app.state.backend.search(Search(status=None)) == []


def test_foreign_scope_cannot_reuse_seed_uuid(app, tmp_path, scope):
    raw = seed()
    migrate(app, bundle(tmp_path, [raw]))
    result = migrate(app, bundle(tmp_path, [{**raw, "user_id": "other"}], name="other.zip"))
    assert result["failed"] == 1 and result["errors"][0]["code"] == "seed_scope"
    assert app.state.backend.get(raw["memory_id"], scope).user_id == "default-user"


def converted_message(**changes):
    return {
        "record_type": "conversation_message",
        "schema_version": "0.1.0",
        "source_type": "chatgpt_export",
        "source_file": "export/conversations.json",
        "conversation_id": "synthetic-c",
        "conversation_title": "合成标题",
        "message_id": "u1",
        "role": "user",
        "content": "我目前使用合成相机 B",
        "created_at": "2026-10-01T12:00:00+08:00",
        "branch_policy": "active_branch",
        "can_be_user_fact": True,
        "evidence_type": "verbatim_export_text",
        "dedup_key": "synthetic-c/u1",
        **changes,
    }


def test_conversation_jsonl_preserves_time_roles_titles_search_and_versions(client, app):
    a = converted_message(
        message_id="a1",
        role="assistant",
        content="助手猜测",
        can_be_user_fact=True,
        created_at="2026-10-01T13:00:00+08:00",
    )
    u = converted_message()
    data = (dumps(a) + "\n" + dumps(u) + "\n").encode()
    job = import_data(client, data, "chatgpt_messages.jsonl")
    assert job["inserted"] == 2 and job["conversations"][0]["title"] == "合成标题"
    assert import_data(client, data, "chatgpt_messages.jsonl")["duplicates"] == 2
    messages = client.get("/v1/messages").json()["items"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["timestamp"] == "2026-10-01T04:00:00+00:00"
    assert messages[1]["metadata"]["can_be_user_fact"] is False
    assert messages[0]["metadata"]["dedup_key"] == u["dedup_key"]
    assert len(client.get("/v1/messages?query=相机").json()["items"]) == 1
    assert client.get("/v1/system").json()["counts"]["conversations"] == 1
    u["source_file"] = "renamed/conversations.json"
    assert import_data(client, (dumps(u) + "\n").encode(), "renamed.jsonl")["duplicates"] == 1
    u["content"] = "我换用了合成相机 C"
    assert import_data(client, (dumps(u) + "\n").encode(), "changed.jsonl")["inserted"] == 1
    assert app.state.models.get().provider == "none"


def test_raw_conflict_with_summary_is_reviewed_and_superseded_only_by_human(client, app, tmp_path, scope):
    raw = seed()
    migrate(app, bundle(tmp_path, [raw]))
    import_data(client, (dumps(converted_message()) + "\n").encode(), "chatgpt_messages.jsonl")
    message = client.get("/v1/messages").json()["items"][0]
    candidate = client.post("/v1/extractions/quote/" + message["key"], json={}).json()
    assert candidate["status"] == "pending_review" and candidate["summary_links"] == [raw["memory_id"]]
    assert candidate["conflicts"] == [raw["memory_id"]]
    assert app.state.backend.get(raw["memory_id"], scope).version == 1
    assert client.post(f"/v1/memories/{candidate['memory_id']}/approve", json={}).status_code == 409
    approved = client.post(
        f"/v1/memories/{candidate['memory_id']}/approve", json={"supersedes": raw["memory_id"]}
    ).json()
    assert approved["status"] == "approved" and approved["supersedes"] == raw["memory_id"]
    old = app.state.backend.get(raw["memory_id"], scope)
    assert old.status == "superseded" and old.version == 2 and old.source_artifact is not None
    assert [m["memory_id"] for m in app.state.backend.search(Search())] == [candidate["memory_id"]]


def test_local_model_can_link_differently_worded_summary_and_cloud_switch_never_sends(
    client,
    app,
    tmp_path,
    monkeypatch,
):
    raw = seed(content="用户计划使用合成设备 A", claim_kind="user_plan")
    migrate(app, bundle(tmp_path, [raw]))
    import_data(client, (dumps(converted_message()) + "\n").encode(), "chatgpt_messages.jsonl")
    app.state.models.save(ModelConfig(provider="ollama", model="synthetic-local"))
    calls = []

    class Provider:
        def complete(self, messages, **kw):
            calls.append(messages)
            assert raw["memory_id"] in messages[1]["content"]
            return dumps(
                {
                    "memories": [
                        {
                            "content": "合成相机已改用 B",
                            "evidence": "使用合成相机 B",
                            "summary_links": [raw["memory_id"]],
                        }
                    ]
                }
            )

    monkeypatch.setattr(app.state.models, "provider", lambda: Provider())
    assert client.post("/v1/extractions/run", json={}).json()["proposed"] == 1
    candidate = app.state.backend.search(Search(status="pending_review"))[0]
    assert candidate["summary_links"] == [raw["memory_id"]] and candidate["evidence_verified"]
    import_data(
        client,
        (dumps(converted_message(message_id="u2", content="合成私密资料")) + "\n").encode(),
        "next.jsonl",
    )
    app.state.models.save(
        ModelConfig(
            provider="openai", model="synthetic-cloud", base_url="https://cloud.example/v1", allow_cloud=True
        )
    )
    preview = client.post("/v1/extractions/preview", json={}).json()
    assert client.post("/v1/extractions/run", json={}).status_code == 409
    assert (
        client.post("/v1/extractions/run", json={"preview_hash": preview["preview_hash"]}).status_code == 409
    )
    assert len(calls) == 1


def test_summary_cannot_be_linked_without_raw_evidence(app, tmp_path, scope):
    raw = seed()
    migrate(app, bundle(tmp_path, [raw]))
    with pytest.raises(ServiceError, match="先保存并核验用户原文"):
        app.state.backend.propose(Proposal(content="虚构", summary_links=[raw["memory_id"]]), scope)


def test_rejected_summary_no_longer_blocks_raw_evidence_review(client, app, tmp_path, scope):
    raw = seed()
    migrate(app, bundle(tmp_path, [raw]))
    import_data(client, (dumps(converted_message()) + "\n").encode(), "chatgpt_messages.jsonl")
    message = client.get("/v1/messages").json()["items"][0]
    candidate = client.post("/v1/extractions/quote/" + message["key"], json={}).json()
    app.state.backend.transition(raw["memory_id"], scope, "rejected")
    response = client.post(f"/v1/memories/{candidate['memory_id']}/approve", json={})
    assert response.status_code == 200 and response.json()["conflicts"] == []


def test_migration_backup_restore_preserves_provenance_and_identity(app, tmp_path, scope):
    raw = seed()
    migrate(app, bundle(tmp_path, [raw]))
    backups = Backups(app.state.store, app.state.importer.settings)
    b = backups.create()
    with (app.state.store.home / "backups" / b["filename"]).open("rb") as source:
        restored = backups.restore(source)
    other = Store(Path(restored["restored_home"]))
    memory = SQLiteBackend(other).get(raw["memory_id"], scope)
    assert memory.source_artifact == app.state.backend.get(raw["memory_id"], scope).source_artifact
    assert Identity(other).get()["migration"] == app.state.identity.get()["migration"]
    assert SQLiteBackend(other)._verify(None, memory, scope) is False


def test_offline_cli_uses_same_store_and_has_no_model_dependency(tmp_path):
    path = tmp_path / "chatgpt_messages.jsonl"
    path.write_text(dumps(converted_message()) + "\n", encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "zero_memory.importers",
        "--input",
        str(path),
        "--home",
        str(tmp_path / "cli-home"),
    ]
    first = subprocess.run(command, capture_output=True, check=True)
    second = subprocess.run(command, capture_output=True, check=True)
    assert json.loads(first.stdout)["inserted"] == json.loads(second.stdout)["duplicates"] == 1


def test_offline_bundle_migration_does_not_use_network(app, tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("network disallowed")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    assert migrate(app, bundle(tmp_path, [seed()]))["inserted"] == 1


def test_model_switch_during_batch_keeps_authorized_local_provider(client, app, monkeypatch):
    messages = [converted_message(message_id="u1"), converted_message(message_id="u2")]
    import_data(client, "".join(dumps(m) + "\n" for m in messages).encode(), "chatgpt_messages.jsonl")
    app.state.models.save(ModelConfig(provider="ollama", model="synthetic-local"))
    destinations = []

    class Provider:
        def complete(self, messages, **kw):
            app.state.models.save(
                ModelConfig(
                    provider="openai",
                    model="synthetic-cloud",
                    base_url="https://cloud.example/v1",
                    allow_cloud=True,
                )
            )
            return dumps({"memories": []})

    def provider():
        destinations.append(app.state.models.get().model)
        return Provider()

    monkeypatch.setattr(app.state.models, "provider", provider)
    result = client.post("/v1/extractions/run", json={}).json()
    assert result["processed"] == 2 and destinations == ["synthetic-local"]


def test_reviewed_summary_claim_kind_stays_visible_in_context(app, tmp_path, scope):
    raw = seed(claim_kind="user_plan")
    migrate(app, bundle(tmp_path, [raw]))
    app.state.backend.approve(raw["memory_id"], scope, confirm_external=True)
    context = app.state.context.build(scope, "合成相机")
    assert context["memories"][0]["claim_kind"] == "user_plan"
    assert context["memories"][0]["origin_quality"] == "summary_only"
    assert context["memories"][0]["evidence_verified"] is False
    assert "历史导入不能冒充零的亲身经历" in context["messages"][0]["content"]


@pytest.mark.parametrize("original,precision", [("2019", "year"), ("2026-09", "month")])
def test_partial_seed_dates_preserve_precision_without_invented_day(app, tmp_path, scope, original, precision):
    raw = seed(event_time=original)
    result = migrate(app, bundle(tmp_path, [raw]))
    assert result["inserted"] == 1 and result["failed"] == 0
    memory = app.state.backend.get(raw["memory_id"], scope)
    assert memory.event_time is None
    assert memory.event_time_original == original and memory.event_time_precision == precision


def test_invalid_partial_calendar_date_is_reported(app, tmp_path):
    report = migrate(app, bundle(tmp_path, [seed(event_time="2026-99")]))
    assert report["failed"] == 1 and report["inserted"] == 0
