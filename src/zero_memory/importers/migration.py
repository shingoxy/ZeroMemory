"""Explicit, offline import of the general migration ZIP into the existing store."""

import argparse
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from zero_memory.core.config import Settings
from zero_memory.core.errors import ServiceError
from zero_memory.core.identity import Identity
from zero_memory.core.store import Store, atomic_json, digest, dumps, now
from zero_memory.importers.chatgpt import safe_zip, timestamp
from zero_memory.memory.sqlite import SQLiteBackend
from zero_memory.schemas.models import Memory, Scope, SourceArtifact

PREFIX = "ZeroMemory_Chat_Migration_Kit/"
SEED = "seed/memories_pending_review.jsonl"
MEMBERS = {
    "Codex_Import_Task.md",
    "README.md",
    "assets/zero-avatar.png",
    "data_dictionary.json",
    "manifest.json",
    SEED,
    "seed_summary.md",
    "tests/test_converter.py",
    "tools/convert_chatgpt_export.py",
    "zero_identity.json",
}


def seed_memory(raw, artifact):
    UUID(raw["memory_id"])
    scope = Scope(**{k: raw[k] for k in Scope.model_fields})
    if raw["source_type"] != "chatgpt_conversation_summary" or raw["origin_quality"] != "summary_only":
        raise ServiceError("seed_origin", "种子必须明确标记为摘要转述")
    if raw.get("source_message_ids") or raw.get("source_message_keys"):
        raise ServiceError("seed_origin", "摘要不能冒充原始消息证据")
    value = dict(raw)
    value.update(
        status="pending_review",
        version=1,
        supersedes=None,
        evidence_verified=False,
        sensitivity="private" if raw["sensitivity"] == "low" else raw["sensitivity"],
        inference_type="assistant_inference"
        if raw["claim_kind"] == "assistant_proposal"
        else "system_summary",
        source_artifact=artifact,
        dedup_key=raw.get("dedup_key")
        or digest(
            dumps(
                [
                    *scope.sql(),
                    raw["memory_type"],
                    raw["content"].strip().casefold(),
                    raw["claim_kind"],
                    raw["source_role"],
                    raw.get("topic"),
                ]
            )
        ),
    )
    event_time = value.get("event_time")
    if event_time is not None:
        value["event_time_original"] = str(event_time)
        partial = isinstance(event_time, str) and re.fullmatch(r"\d{4}(?:-\d{2})?", event_time)
        if partial:
            precision = "year" if len(event_time) == 4 else "month"
            datetime.strptime(event_time, "%Y" if precision == "year" else "%Y-%m")
            value["event_time"], value["event_time_precision"] = None, precision
        else:
            value["event_time"] = timestamp(event_time)
            value["event_time_precision"] = "day" if len(str(event_time)) == 10 else "instant"
    for field in ("valid_from", "valid_to", "extracted_at"):
        value[field] = timestamp(value.get(field))
    return Memory.model_validate(value)


def persist_source(store, data, suffix):
    checksum = digest(data)
    target = store.home / "sources" / (checksum + suffix)
    with store.lock():
        if target.exists():
            if digest(target.read_bytes()) != checksum:
                raise ServiceError("source_integrity", "已有来源文件校验失败，拒绝覆盖")
        else:
            temp = target.with_suffix(suffix + ".tmp")
            temp.write_bytes(data)
            temp.replace(target)
    return target, checksum


def import_bundle(path: Path, store: Store, settings: Settings):
    # Never discover adjacent ZIPs or execute any code from a migration archive.
    if path.stat().st_size > settings.max_upload_bytes:
        raise ServiceError("upload_limit", "迁移包超过本地导入大小限制")
    with safe_zip(path, settings) as archive:
        names = {i.filename for i in archive.infolist() if not i.is_dir()}
        if names != {PREFIX + n for n in MEMBERS}:
            raise ServiceError("bundle_members", "只接受标准通用迁移包；敏感包和额外成员不会读取")
        manifest = json.loads(archive.read(PREFIX + "manifest.json"))
        if manifest.get("bundle") != "ZeroMemory_Chat_Migration_Kit":
            raise ServiceError("bundle_type", "不是通用迁移包")
        files = manifest["files"]
        if set(files) != MEMBERS - {"manifest.json"}:
            raise ServiceError("bundle_manifest", "迁移包清单不完整")
        payload = {}
        for name in files:
            data = archive.read(PREFIX + name)
            if len(data) != files[name]["bytes"] or digest(data) != files[name]["sha256"]:
                raise ServiceError("bundle_integrity", "迁移包成员校验失败")
            payload[name] = data
    identity_raw = json.loads(payload["zero_identity.json"])
    if (
        not isinstance(identity_raw, dict)
        or identity_raw.get("agent_id") != "zero-001"
        or not isinstance(identity_raw.get("persona"), dict)
    ):
        raise ServiceError("identity_agent", "迁移身份必须为 zero-001，且包含有效 persona")
    lines = payload[SEED].decode("utf-8-sig").splitlines()
    if sum(bool(line.strip()) for line in lines) != manifest["records_general"]:
        raise ServiceError("seed_count", "种子条数与清单不一致")
    # Preserve the exact ZIP and JSONL before changing identity or memory versions.
    _, bundle_hash = persist_source(store, path.read_bytes(), ".zip")
    seed_path, seed_hash = persist_source(store, payload[SEED], ".jsonl")
    report = {
        "bundle_hash": bundle_hash,
        "seed_hash": seed_hash,
        "started_at": now(),
        "inserted": 0,
        "duplicates": 0,
        "failed": 0,
        "skipped_sensitive": 0,
        "errors": [],
        "sensitivity_mapping": {"low": "private"},
        "private_bundle_read": False,
    }
    backend = SQLiteBackend(store)
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if raw.get("sensitivity") == "sensitive":
                report["skipped_sensitive"] += 1
                continue
            artifact = SourceArtifact(
                source_file=seed_path.name,
                source_hash=seed_hash,
                line=number,
                record_hash=digest(dumps(raw)),
                bundle_hash=bundle_hash,
                member=SEED,
            )
            memory = seed_memory(raw, artifact)
            with store.write() as db:
                rows = db.execute(
                    "SELECT body FROM memories WHERE id=? OR "
                    "(user_id=? AND agent_id=? AND project_id=? AND json_extract(body,'$.dedup_key')=?)",
                    (memory.memory_id, memory.user_id, memory.agent_id, memory.project_id, memory.dedup_key),
                ).fetchall()
                if rows:
                    for row in rows:
                        old = Memory.model_validate_json(row[0])
                        if Scope(**{k: getattr(old, k) for k in Scope.model_fields}) != Scope(
                            **{k: getattr(memory, k) for k in Scope.model_fields}
                        ):
                            raise ServiceError("seed_scope", "UUID 已属于其他作用域")
                        if old.source_artifact is None:
                            raise ServiceError("seed_changed", "已有同名记忆，需人工核对来源")
                        if (
                            old.memory_id == memory.memory_id
                            and old.source_artifact.record_hash != artifact.record_hash
                        ):
                            raise ServiceError(
                                "seed_changed", "同一 UUID 的摘要已变化，保留旧版并报告待人工核对"
                            )
                        if old.dedup_key != memory.dedup_key:
                            raise ServiceError("seed_changed", "同一 UUID 的去重键变化，需人工核对")
                        if old.memory_id != memory.memory_id:
                            source = store.home / "sources" / old.source_artifact.source_file
                            original = json.loads(
                                source.read_text(encoding="utf-8-sig").splitlines()[
                                    old.source_artifact.line - 1
                                ]
                            )
                            if {k: v for k, v in original.items() if k != "memory_id"} != {
                                k: v for k, v in raw.items() if k != "memory_id"
                            }:
                                raise ServiceError("seed_changed", "同一去重键的摘要变化，需人工核对")
                    report["duplicates"] += 1
                else:
                    backend._verify(db, memory, Scope(**{k: getattr(memory, k) for k in Scope.model_fields}))
                    backend._put(db, memory)
                    store.audit(db, "seed_import", "migration", memory.memory_id, artifact.model_dump())
                    report["inserted"] += 1
        except (ValueError, KeyError, TypeError, ServiceError) as exc:
            report["failed"] += 1
            report["errors"].append({"line": number, "code": getattr(exc, "code", "seed_schema")})
    identity = Identity(store)
    with store.lock():
        avatar = payload["assets/zero-avatar.png"]
        asset = store.home / "identity" / "assets" / ("zero-avatar-" + digest(avatar) + ".png")
        asset.parent.mkdir(exist_ok=True)
        if not asset.exists():
            asset.write_bytes(avatar)
        elif digest(asset.read_bytes()) != digest(avatar):
            raise ServiceError("avatar_integrity", "已有受控头像校验失败")
        old = identity.get()
        metadata = {
            "memory_name": identity_raw.get("memory_name"),
            "memory_name_en": identity_raw.get("memory_name_en"),
            "identity_start_date": identity_raw.get("identity_start_date"),
            "memory_semantics": identity_raw.get("memory_semantics"),
            "memory_import_policy": identity_raw.get("memory_import_policy"),
            "user_confirmed_fields": identity_raw["persona"].get("user_confirmed_fields", []),
            "persona_proposals_pending_review": identity_raw["persona"].get("proposed_traits_for_review", []),
            "provenance": identity_raw.get("provenance"),
            "source_bundle_hash": bundle_hash,
            "avatar": asset.relative_to(store.home / "identity").as_posix(),
        }
        merged = {**metadata, **old.get("migration", {})}
        if merged != old.get("migration"):
            value = {
                **old,
                "migration": merged,
                "version": old["version"] + 1,
                "updated_at": now(),
                "reason": "migration_metadata",
            }
            identity._save(value)
            with store.write() as db:
                store.audit(
                    db,
                    "identity_migration",
                    "migration",
                    str(value["version"]),
                    {"bundle_hash": bundle_hash, "existing_soul_style_preserved": True},
                )
        report["identity_version"] = identity.get()["version"]
    with store.read() as db:
        report["pending_review"] = db.execute(
            "SELECT count(*) FROM memories WHERE status='pending_review'"
        ).fetchone()[0]
        report["raw_conversations"] = db.execute(
            "SELECT count(*) FROM (SELECT DISTINCT user_id,agent_id,project_id,conversation_id FROM messages)"
        ).fetchone()[0]
        report["raw_messages"] = db.execute("SELECT count(*) FROM messages").fetchone()[0]
    report["completed_at"] = now()
    report_path = store.home / "review_queue" / ("migration-" + now().replace(":", "-") + ".json")
    atomic_json(report_path, report)
    return {**report, "report_path": str(report_path)}


def main():
    parser = argparse.ArgumentParser(description="离线导入通用迁移包，所有种子记忆进入待审核")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path("data"))
    args = parser.parse_args()
    try:
        settings = Settings(_env_file=None, home=args.home, llm_provider="none", allow_cloud=False)
        result = import_bundle(args.bundle, Store(settings.home), settings)
        print(dumps(result))
        return int(result["failed"] > 0)
    except (
        ServiceError,
        ValidationError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OSError,
        zipfile.BadZipFile,
    ):
        print("迁移失败：请检查通用包的格式、校验值和本地目录权限；未读取相邻敏感包。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
