"""Confirmed raw history import, independent of extraction and memory search."""

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path, PurePosixPath
from uuid import UUID

import portalocker

from zero_memory.core.config import Settings
from zero_memory.core.errors import ServiceError
from zero_memory.core.store import Store, digest, dumps, now
from zero_memory.importers.chatgpt import normalize_message, safe_zip, timestamp
from zero_memory.importers.openai_preview import (
    VOLUMES,
    PreviewLimits,
    _content,
    _conversations,
    _graph,
    file_sha256,
)
from zero_memory.importers.service import Importer
from zero_memory.schemas.models import Scope

RAW_TABLES = {
    "raw_batches": ("id", "body"),
    "raw_batch_items": ("batch_id", "ordinal", "body"),
    "raw_conversations": ("id", "batch_id", "user_id", "agent_id", "project_id", "conversation_id", "body"),
    "raw_messages": ("snapshot_id", "node_id", "message_key", "body"),
    "raw_owned": ("batch_id", "kind", "id"),
}
DDL = (
    "CREATE TABLE IF NOT EXISTS raw_batches(id TEXT PRIMARY KEY,body TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS raw_batch_items(batch_id TEXT,ordinal INTEGER,body TEXT,PRIMARY KEY(batch_id,ordinal))",
    "CREATE TABLE IF NOT EXISTS raw_conversations(id TEXT PRIMARY KEY,batch_id TEXT,user_id TEXT,agent_id TEXT,project_id TEXT,conversation_id TEXT,body TEXT)",
    "CREATE TABLE IF NOT EXISTS raw_messages(snapshot_id TEXT,node_id TEXT,message_key TEXT,body TEXT,PRIMARY KEY(snapshot_id,node_id))",
    "CREATE TABLE IF NOT EXISTS raw_owned(batch_id TEXT,kind TEXT,id TEXT,PRIMARY KEY(batch_id,kind,id))",
    "CREATE INDEX IF NOT EXISTS raw_conversations_scope ON raw_conversations(user_id,agent_id,project_id,conversation_id)",
    "CREATE INDEX IF NOT EXISTS raw_messages_key ON raw_messages(message_key)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS raw_history_fts USING fts5(snapshot_id UNINDEXED,content,tokenize='trigram')",
)


def initialized(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_batches'").fetchone() is not None


def install(db):
    if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 2):
        raise ServiceError("schema_version", "未知数据库版本")
    for statement in DDL:
        db.execute(statement)
    for table, columns in RAW_TABLES.items():
        if tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})")) != columns:
            raise ServiceError("raw_schema", "原始历史表结构不兼容")


def rebuild_index(db):
    db.execute("DELETE FROM raw_history_fts")
    db.execute("INSERT INTO raw_history_fts SELECT snapshot_id,json_extract(body,'$.content_text') FROM raw_messages")


def load_preview(home, preview_id):
    ident = str(UUID(str(preview_id)))
    path = Path(home) / "review_queue/v03a" / (ident + ".json")
    preview = json.loads(path.read_text(encoding="utf-8"))
    if (preview.get("preview_id") != ident or preview.get("format") != "zero-memory-openai-preview/v0.3a"
            or preview.get("status") != "awaiting_confirmation"):
        raise ServiceError("preview_invalid", "预览编号无效或已被替换")
    return preview


def _source_ref(sha, member, pointer):
    return {"zip_sha256": sha, "member_name": member, "json_pointer": pointer}


def _escape_pointer(value):
    return value.replace("~", "~0").replace("/", "~1")


def content_text(message):
    """Keep scalar text or verbatim text segments. Non-text descriptors stay in raw metadata."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    if content.get("content_type") == "reasoning_recap" and isinstance(content.get("content"), str):
        return content["content"]
    if content.get("content_type") == "thoughts" and isinstance(content.get("thoughts"), list):
        return "\n".join(thought["content"] for thought in content["thoughts"]
                         if isinstance(thought, dict) and isinstance(thought.get("content"), str))
    for field in ("text", "result"):
        if isinstance(content.get(field), str):
            return content[field]
    parts = content.get("parts")
    return "\n".join(part for part in parts if isinstance(part, str)) if isinstance(parts, list) else ""


def _attachments(message, members):
    content = message.get("content")
    parts = content.get("parts", []) if isinstance(content, dict) else []
    metadata = message.get("metadata") or {}
    attachments = metadata.get("attachments", []) if isinstance(metadata, dict) else []
    result = []
    for entries in (parts, attachments):
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            pointer = item.get("asset_pointer") or item.get("id") or item.get("file_id")
            name = pointer.split("://", 1)[-1] if isinstance(pointer, str) else None
            match = members.get(name) if name else None
            checksum = item.get("sha256")
            result.append({"content_type": item.get("content_type") or item.get("mime_type"),
                           "asset_pointer": pointer, "member_name": match.filename if match else None,
                           "size_bytes": match.file_size if match else item.get("size_bytes", item.get("size")),
                           "sha256": checksum if isinstance(checksum, str) and re.fullmatch(r"[0-9a-fA-F]{64}", checksum) else None})
    return result


def _positions(graph, main):
    children = {}
    for node_id, node in graph.items():
        children.setdefault(node.get("parent"), []).append(node_id)
    order = {}
    for parent, ids in children.items():
        declared = graph[parent].get("children", []) if parent is not None else []
        indexed = {node: i for i, node in enumerate(declared)}
        ids.sort(key=lambda node: indexed.get(node, len(indexed)))
        order.update({node: i for i, node in enumerate(ids)})
    positions = {}
    stack = [(node, "", 0) for node in reversed(children.get(None, []))]
    while stack:
        node, branch, depth = stack.pop()
        parent = graph[node].get("parent")
        if node in main:
            branch = "main"
        elif branch in ("", "main") or len(children[parent]) > 1:
            branch = "branch:" + node
        positions[node] = (branch, depth, order[node])
        stack.extend((child, branch, depth + 1) for child in reversed(children.get(node, [])))
    return positions


def _save_job(db, job):
    db.execute("INSERT OR REPLACE INTO raw_batches VALUES(?,?)", (job["id"], dumps(job)))


def _message_record(db, scope, sid, cid, node_id, record, message, details, batch_id):
    key, legacy_hash = Importer.key(record, scope)
    # v0.2 rows stay byte-identical. Match their existing stable key before adding a row.
    exact = db.execute("SELECT 1 FROM messages WHERE key=?", (key,)).fetchone()
    fingerprint = digest(dumps([record["role"], message.get("content")]))
    previous = [json.loads(row[0]) for row in db.execute(
        "SELECT m.body FROM raw_messages m JOIN raw_conversations c ON c.id=m.snapshot_id "
        "WHERE c.user_id=? AND c.agent_id=? AND c.project_id=? AND c.conversation_id=? AND m.node_id=?",
        (*scope.sql(), cid, node_id),
    )]
    same = next((item for item in previous if item["content_fingerprint"] == fingerprint), None)
    version = same["message_version"] if same else 1 + max((item["message_version"] for item in previous), default=0)
    if same:
        key = same["message_key"]
    elif not exact:
        stored = {**record, "content": details["content_text"], "source_ref": details["source_ref"], "import_id": batch_id}
        db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            key, *scope.sql(), cid, record["message_id"], legacy_hash, record["role"], record["timestamp"],
            details["content_text"], dumps(stored),
        ))
        db.execute("INSERT INTO raw_owned VALUES(?,?,?)", (batch_id, "message", key))
    details.update(content_fingerprint=fingerprint, message_version=version, message_key=key)
    db.execute("INSERT INTO raw_messages VALUES(?,?,?,?)", (sid, node_id, key, dumps(details)))
    db.execute("INSERT INTO raw_history_fts VALUES(?,?)", (sid, details["content_text"]))
    return not (same or exact)


def _conversation(db, scope, job, item, member, pointer, members):
    if not isinstance(item, dict) or not isinstance(item.get("mapping"), dict):
        return {"status": "skipped_unsupported", "skipped": 0, "reason": "conversation_structure"}
    cid = str(item.get("conversation_id") or item.get("id") or digest(dumps(item)))
    graph = item["mapping"]
    message_count = sum(isinstance(node, dict) and node.get("message") is not None for node in graph.values())
    issues = Counter()
    main = _graph(graph, item.get("current_node"), issues)
    if any(issues[k] for k in ("parent_cycle", "missing_parent", "invalid_parent", "invalid_node", "invalid_children")):
        return {"status": "failed", "failed": message_count, "reason": "invalid_message_tree"}
    positions = _positions(graph, main or set())
    sid = digest(dumps([*scope.sql(), job["zip_sha256"], cid, member, pointer]))
    if db.execute("SELECT 1 FROM raw_conversations WHERE id=?", (sid,)).fetchone():
        unsupported = db.execute("SELECT count(*) FROM raw_messages WHERE snapshot_id=? AND json_extract(body,'$.unsupported')", (sid,)).fetchone()[0]
        return {"status": "skipped_duplicate", "duplicates": message_count, "unsupported": unsupported}
    ref = _source_ref(job["zip_sha256"], member, pointer)
    conversation = {"conversation_id": cid, "title": item.get("title"), "current_node": item.get("current_node"),
                    "created_at": timestamp(item.get("create_time")), "updated_at": timestamp(item.get("update_time")),
                    "source_type": "chatgpt_export", "source_ref": ref}
    new_conversation = db.execute("INSERT OR IGNORE INTO conversations VALUES(?,?,?,?,?)", (
        *scope.sql(), cid, dumps({**conversation, "kind": "conversation"}),
    )).rowcount
    if new_conversation:
        db.execute("INSERT INTO raw_owned VALUES(?,?,?)", (job["id"], "conversation", cid))
    db.execute("INSERT INTO raw_conversations VALUES(?,?,?,?,?,?,?)", (sid, job["id"], *scope.sql(), cid, dumps(conversation)))
    counts = Counter()
    for pos, (node_id, node) in enumerate(graph.items()):
        message = node.get("message")
        if message is None:
            continue
        record = normalize_message(message, cid, pos, parent=node.get("parent"), node_id=node_id, source_type="chatgpt_export")
        kind, unsupported, _ = _content(message)
        meta = message.get("metadata") or {}
        author = message.get("author") or {}
        branch, depth, sibling = positions[node_id]
        parent = node.get("parent")
        details = {"message_id": node_id, "source_message_id": record["message_id"], "conversation_id": cid,
                   "parent_message_id": parent, "branch_id": branch, "is_main_branch": main is not None and node_id in main,
                   "depth": depth, "sibling_index": sibling, "role": record["role"], "author_name": author.get("name"),
                   "observed_at": record["timestamp"], "create_time_original": message.get("create_time"),
                   "model_slug": meta.get("model_slug"), "content_type": kind, "content_text": content_text(message),
                   "is_hidden": meta.get("is_visually_hidden_from_conversation") is True or message.get("is_hidden") is True,
                   "is_system": record["role"] == "system", "unsupported": unsupported,
                   "attachments": _attachments(message, members),
                   "source_ref": _source_ref(job["zip_sha256"], member, f"{pointer}/mapping/{_escape_pointer(node_id)}/message")}
        inserted = _message_record(db, scope, sid, cid, node_id, record, message, details, job["id"])
        counts["inserted" if inserted else "duplicates"] += 1
        counts["unsupported"] += unsupported
    return {"status": "imported" if new_conversation or counts["inserted"] else "skipped_duplicate",
            "metadata_added": 1, **counts}


def _scan(path, limits):
    settings = Settings(_env_file=None, llm_provider="none", allow_cloud=False,
                        max_member_bytes=limits.max_member_bytes, max_expanded_bytes=limits.max_expanded_bytes)
    with safe_zip(path, settings, max_files=limits.max_members, max_ratio=limits.max_ratio) as archive:
        members = {}
        selected = []
        for info in archive.infolist():
            name = PurePosixPath(info.filename.replace("\\", "/")).name
            if VOLUMES.fullmatch(name) and not info.is_dir():
                selected.append(info.filename)
            elif not info.is_dir():
                for alias in {name, PurePosixPath(name).stem}:
                    members[alias] = info if alias not in members else None
        for member in sorted(selected):
            with archive.open(member) as source:
                for pointer, item in _conversations(source):
                    if len(dumps(item).encode()) > limits.max_conversation_bytes:
                        raise ServiceError("conversation_limit", "单会话超过预览限制")
                    yield item, member, pointer, members


def run_confirmed(store, preview, confirmation):
    ident = str(UUID(preview["preview_id"]))
    if confirmation != f"确认导入 {ident}" or preview.get("status") != "awaiting_confirmation":
        raise ServiceError("confirmation_required", "需要匹配预览编号的确认")
    path = Path(preview["source_path"])
    scope = Scope.model_validate(preview["scope"])
    limits = PreviewLimits(**preview["limits"])
    source_stat = path.stat()
    if source_stat.st_size != preview["zip_bytes"] or file_sha256(path) != preview["zip_sha256"]:
        raise ServiceError("source_changed", "ZIP 与预览不一致")
    with portalocker.Lock(str(store.home / ".raw-import.lock"), timeout=1):
        return _run(store, preview, path, source_stat, scope, limits, ident)


def _run(store, preview, path, source_stat, scope, limits, ident):
    with store.write() as db:
        install(db)
        prior = db.execute("SELECT body FROM raw_batches WHERE id=?", (ident,)).fetchone()
        job = json.loads(prior[0]) if prior else {"id": ident, "scope": scope.model_dump(), "created_at": now(),
            "zip_sha256": preview["zip_sha256"], "cursor": 0, "conversations": {}, "messages": {}}
        if job["zip_sha256"] != preview["zip_sha256"] or job["scope"] != scope.model_dump():
            raise ServiceError("preview_invalid", "批次绑定不匹配")
        if job.get("status") == "completed":
            return job
        if job.get("status") == "rolled_back":
            raise ServiceError("batch_rolled_back", "已回滚批次须重新预览")
        job.update(status="running", updated_at=now(), error=None)
        _save_job(db, job)
    try:
        for ordinal, (item, member, pointer, members) in enumerate(_scan(path, limits)):
            if ordinal < job["cursor"]:
                continue
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (source_stat.st_size, source_stat.st_mtime_ns, source_stat.st_ino):
                raise ServiceError("source_changed", "导入期间 ZIP 变化")
            with store.write() as db:
                db.execute("SAVEPOINT raw_item")
                try:
                    result = _conversation(db, scope, job, item, member, pointer, members)
                except (ServiceError, AttributeError, TypeError, ValueError):
                    db.execute("ROLLBACK TO raw_item")
                    graph = item.get("mapping", {}) if isinstance(item, dict) else {}
                    failed = sum(isinstance(n, dict) and n.get("message") is not None for n in graph.values())
                    result = {"status": "failed", "failed": failed, "reason": "invalid_message"}
                db.execute("RELEASE raw_item")
                db.execute("INSERT INTO raw_batch_items VALUES(?,?,?)", (ident, ordinal, dumps(result)))
                job["conversations"] = dict(Counter(job["conversations"]) + Counter({result["status"]: 1}))
                increments = {k: v for k, v in result.items() if type(v) is int}
                job["messages"] = dict(Counter(job["messages"]) + Counter(increments))
                job.update(cursor=ordinal + 1, updated_at=now())
                _save_job(db, job)
        if file_sha256(path) != preview["zip_sha256"]:
            raise ServiceError("source_changed", "ZIP 校验失败")
        observed = sum(job["messages"].get(k, 0) for k in ("inserted", "duplicates", "failed", "skipped"))
        if observed != preview["counts"].get("messages", 0) or job["cursor"] != preview["counts"]["conversation_records"]:
            raise ServiceError("count_mismatch", "批次与预览对账失败")
        job["status"] = "completed"
    except Exception as exc:
        job["status"] = "interrupted"
        job["error"] = exc.code if isinstance(exc, ServiceError) else "import_interrupted"
    with store.write() as db:
        job["updated_at"] = now()
        _save_job(db, job)
    return job


def rollback_batch(store, batch_id, confirmation=None):
    batch_id = str(UUID(batch_id))
    with portalocker.Lock(str(store.home / ".raw-import.lock"), timeout=1), store.write() as db:
        row = db.execute("SELECT body FROM raw_batches WHERE id=?", (batch_id,)).fetchone() if initialized(db) else None
        if not row:
            raise ServiceError("not_found", "原始历史批次不存在", 404)
        job = json.loads(row[0])
        scope = Scope.model_validate(job["scope"])
        snapshots = "SELECT id FROM raw_conversations WHERE batch_id=?"
        owned = {kind: [r[0] for r in db.execute("SELECT id FROM raw_owned WHERE batch_id=? AND kind=?", (batch_id, kind))]
                 for kind in ("message", "conversation")}
        counts = {"conversations": len(owned["conversation"]), "messages": len(owned["message"]),
                  "snapshots": db.execute(f"SELECT count(*) FROM ({snapshots})", (batch_id,)).fetchone()[0],
                  "message_metadata": db.execute(f"SELECT count(*) FROM raw_messages WHERE snapshot_id IN ({snapshots})", (batch_id,)).fetchone()[0],
                  "index_rows": db.execute(f"SELECT count(*) FROM raw_history_fts WHERE snapshot_id IN ({snapshots})", (batch_id,)).fetchone()[0]}
        token = f"确认回滚 {batch_id} {digest(dumps([counts, job['updated_at']]))[:16]}"
        if confirmation is None:
            return {"dry_run": True, "id": batch_id, "counts": counts, "confirmation": token}
        if confirmation != token or job["status"] == "rolled_back":
            raise ServiceError("rollback_confirmation", "回滚确认与当前预览不匹配")
        for cid in owned["conversation"]:
            current = db.execute("SELECT body FROM conversations WHERE user_id=? AND agent_id=? AND project_id=? AND id=?",
                                 (*scope.sql(), cid)).fetchone()
            originals = db.execute("SELECT body FROM raw_conversations WHERE batch_id=? AND conversation_id=?", (batch_id, cid))
            if not current or not any(current[0] == dumps({**json.loads(row[0]), "kind": "conversation"}) for row in originals):
                raise ServiceError("rollback_referenced", "会话已被其他导入更新")
        db.execute(f"DELETE FROM raw_history_fts WHERE snapshot_id IN ({snapshots})", (batch_id,))
        db.execute(f"DELETE FROM raw_messages WHERE snapshot_id IN ({snapshots})", (batch_id,))
        db.execute("DELETE FROM raw_conversations WHERE batch_id=?", (batch_id,))
        for key in owned["message"]:
            referenced = db.execute("SELECT 1 FROM raw_messages WHERE message_key=? UNION ALL SELECT 1 FROM extraction_tasks WHERE message_key=?", (key, key)).fetchone()
            evidence = db.execute("SELECT 1 FROM memories WHERE instr(body,?)>0 UNION ALL SELECT 1 FROM revisions WHERE instr(body,?)>0", (key, key)).fetchone()
            if referenced or evidence:
                raise ServiceError("rollback_referenced", "批次消息已被其他记录引用")
            db.execute("DELETE FROM messages WHERE key=?", (key,))
        for cid in owned["conversation"]:
            if db.execute("SELECT 1 FROM messages WHERE user_id=? AND agent_id=? AND project_id=? AND conversation_id=?", (*scope.sql(), cid)).fetchone():
                raise ServiceError("rollback_referenced", "会话已有其他批次消息")
            db.execute("DELETE FROM conversations WHERE user_id=? AND agent_id=? AND project_id=? AND id=?", (*scope.sql(), cid))
        db.execute("DELETE FROM raw_owned WHERE batch_id=?", (batch_id,))
        job.update(status="rolled_back", updated_at=now())
        _save_job(db, job)
        return {"dry_run": False, "id": batch_id, "counts": counts}


def recover_batches(store):
    with store.read() as db:
        if not initialized(db):
            return
    try:
        with portalocker.Lock(str(store.home / ".raw-import.lock"), timeout=0), store.write() as db:
            for row in db.execute("SELECT body FROM raw_batches WHERE json_extract(body,'$.status')='running'").fetchall():
                job = json.loads(row[0])
                job.update(status="interrupted", updated_at=now(), error="process_stopped")
                _save_job(db, job)
    except portalocker.exceptions.LockException:
        return


def main():
    parser = argparse.ArgumentParser(description="本地确认导入或回滚原始对话批次")
    parser.add_argument("--home", type=Path, required=True)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--preview-id")
    commands.add_argument("--rollback-batch")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    try:
        home = args.home.resolve()
        if not (home / "memory/zero.sqlite3").is_file():
            raise ServiceError("database_missing", "需要现有数据库")
        if args.preview_id:
            preview = load_preview(home, args.preview_id)
            if args.confirm != f"确认导入 {preview['preview_id']}":
                raise ServiceError("confirmation_required", "需要匹配预览编号的确认")
            result = run_confirmed(Store(home), preview, args.confirm)
        else:
            result = rollback_batch(Store(home), args.rollback_batch, args.confirm)
        print(dumps(result))
        return int(result.get("status") == "interrupted")
    except (ServiceError, ValueError, KeyError, TypeError, OSError, sqlite3.Error, portalocker.exceptions.LockException) as exc:
        print(dumps({"error": exc.code if isinstance(exc, ServiceError) else "raw_history_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
