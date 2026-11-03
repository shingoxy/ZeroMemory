"""Read-only OpenAI archive statistics. Does not import messages or call models."""

import argparse
import hashlib
import io
import re
import sqlite3
import zipfile
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

import ijson

from zero_memory.core.config import Settings
from zero_memory.core.errors import ServiceError
from zero_memory.core.store import atomic_json, digest, dumps, now
from zero_memory.importers.chatgpt import normalize_message, safe_zip, timestamp
from zero_memory.importers.service import Importer
from zero_memory.schemas.models import Scope

VOLUMES = re.compile(r"conversations(?:[-_]\d+)?\.json$", re.IGNORECASE)
TEXT_TYPES = {"text", "code", "execution_output", "thoughts", "reasoning_recap", "summary"}
METADATA_TYPES = {
    "multimodal_text", "image", "image_asset_pointer", "audio", "audio_asset_pointer", "video",
    "audio_perception", "audio_transcription", "audio_transcription_text", "text_audio",
    "real_time_user_audio_video", "tether_quote", "tether_browsing_display", "computer_output",
    "model_editable_context", "user_editable_context", "system_error", "tool_call", "unsupported",
}


@dataclass(frozen=True)
class PreviewLimits:
    max_archive_bytes: int = 512 * 1024**2
    max_expanded_bytes: int = 1024**3
    max_member_bytes: int = 128 * 1024**2
    max_members: int = 1000
    max_ratio: int = 100
    max_conversation_bytes: int = 32 * 1024**2
    max_total_messages: int = 500_000

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()):
            raise ValueError("Preview limits must be positive integers")


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _conversations(stream):
    """Incremental by conversation, never json.load an entire volume."""
    buffered = io.BufferedReader(stream)
    if buffered.peek(4096).lstrip().startswith(b"\xef\xbb\xbf"):
        buffered.read(3)
    first = buffered.peek(4096).lstrip()
    if first.startswith(b"["):
        for index, item in enumerate(ijson.items(buffered, "item", use_float=True)):
            yield f"/{index}", item
    elif first.startswith(b"{"):
        start = buffered.tell()
        wrapper = False
        for prefix, event, value in ijson.parse(buffered, use_float=True):
            if prefix == "" and event == "map_key":
                if value == "conversations":
                    wrapper = True
                    break
                if value in {"mapping", "conversation_id", "id", "messages"}:
                    break
        buffered.seek(start)
        for index, item in enumerate(ijson.items(buffered, "conversations.item" if wrapper else "", use_float=True)):
            yield f"/conversations/{index}" if wrapper else "", item
    else:
        raise ServiceError("preview_json", "对话卷不是 JSON 数组或对象")


def _graph(graph, current, anomalies):
    done = set()
    for node_id, node in graph.items():
        if not isinstance(node, dict):
            anomalies["invalid_node"] += 1
            continue
        parent = node.get("parent")
        if isinstance(parent, str) and parent not in graph:
            anomalies["missing_parent"] += 1
        children = node.get("children", [])
        if not isinstance(children, list):
            anomalies["invalid_children"] += 1
            continue
        for child in children:
            if not isinstance(child, str) or child not in graph:
                anomalies["missing_child"] += 1
            elif isinstance(graph[child], dict) and graph[child].get("parent") != node_id:
                anomalies["parent_child_mismatch"] += 1
        walk, cursor = set(), node_id
        while cursor in graph and cursor not in done:
            if cursor in walk:
                anomalies["parent_cycle"] += 1
                break
            walk.add(cursor)
            item = graph[cursor]
            if not isinstance(item, dict):
                break
            cursor = item.get("parent")
            if cursor is not None and not isinstance(cursor, str):
                anomalies["invalid_parent"] += 1
                break
        done.update(walk)
    main, cursor = set(), current
    if not isinstance(current, str) or current not in graph:
        anomalies["missing_current_node"] += 1
        return None
    while isinstance(cursor, str) and cursor in graph and cursor not in main:
        main.add(cursor)
        item = graph[cursor]
        if not isinstance(item, dict):
            return None
        cursor = item.get("parent")
    return main if cursor is None else None


def _content(message):
    content = message.get("content")
    if isinstance(content, str):
        return "text", False, not content.strip()
    if not isinstance(content, dict):
        return "unknown", True, True
    kind = content.get("content_type", "text")
    kind = kind if isinstance(kind, str) and kind in TEXT_TYPES | METADATA_TYPES else "unknown"
    parts = content.get("parts", [])
    texts = [p for p in parts if isinstance(p, str)] if isinstance(parts, list) else []
    texts += [content[k] for k in ("text", "result") if isinstance(content.get(k), str)]
    has_nontext = isinstance(parts, list) and any(not isinstance(p, str) for p in parts)
    return kind, kind not in TEXT_TYPES or has_nontext, kind in {"text", "code"} and not any(t.strip() for t in texts)


def _existing(database, scope):
    if database is None:
        return set(), set(), set()
    database = Path(database).resolve()
    if not database.is_file():
        raise ServiceError("preview_database", "必须指定现有数据库；拒绝初始化")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 2}:
            raise ServiceError("schema_version", "未知数据库版本；只读预览已停止")
        rows = db.execute(
            "SELECT key,conversation_id,message_id FROM messages WHERE user_id=? AND agent_id=? AND project_id=?",
            scope.sql(),
        ).fetchall()
        conversations = {r[0] for r in db.execute(
            "SELECT id FROM conversations WHERE user_id=? AND agent_id=? AND project_id=?", scope.sql()
        )}
    return {r[0] for r in rows}, {(r[1], r[2]) for r in rows}, conversations


def preview_archive(path, database=None, scope=None, limits=None):
    path, scope, limits = Path(path).resolve(), scope or Scope(), limits or PreviewLimits()
    if path.suffix.lower() != ".zip" or not path.is_file():
        raise ServiceError("preview_input", "需要用户明确提供的现有 ZIP 文件")
    if path.stat().st_size > limits.max_archive_bytes:
        raise ServiceError("preview_limit", "ZIP 大小超过显式预览限额")
    sha256 = file_sha256(path)
    existing_keys, existing_ids, existing_conversations = _existing(database, scope)
    settings = Settings(_env_file=None, llm_provider="none", allow_cloud=False,
                        max_member_bytes=limits.max_member_bytes, max_expanded_bytes=limits.max_expanded_bytes)
    roles, content_types, unsupported_types, branches, anomalies = (Counter() for _ in range(5))
    unique_ids, keys = set(), set()
    counts = Counter()
    earliest = latest = None
    try:
        with safe_zip(path, settings, max_files=limits.max_members, max_ratio=limits.max_ratio) as archive:
            members = [{"name": i.filename, "compressed_bytes": i.compress_size, "expanded_bytes": i.file_size,
                        "conversation_data": bool(VOLUMES.fullmatch(PurePosixPath(i.filename.replace('\\', '/')).name))}
                       for i in archive.infolist() if not i.is_dir()]
            selected = [i for i in members if i["conversation_data"]]
            if not selected:
                raise ServiceError("preview_volumes", "ZIP 中没有标准 OpenAI 对话卷")
            for member in sorted(selected, key=lambda i: i["name"]):
                # No other member is opened, including user.json, HTML, feedback or attachments.
                with archive.open(member["name"]) as stream:
                    for _, conversation in _conversations(stream):
                        if not isinstance(conversation, dict):
                            anomalies["invalid_conversation"] += 1
                            continue
                        if len(dumps(conversation).encode()) > limits.max_conversation_bytes:
                            raise ServiceError("preview_conversation_limit", "单会话超过显式预览限额")
                        counts["conversation_records"] += 1
                        cid = str(conversation.get("conversation_id") or conversation.get("id") or digest(dumps(conversation)))
                        unique_ids.add(cid)
                        graph = conversation.get("mapping")
                        if not isinstance(graph, dict):
                            anomalies["unsupported_conversation_structure"] += 1
                            continue
                        main = _graph(graph, conversation.get("current_node"), anomalies)
                        for pos, (node_id, node) in enumerate(graph.items()):
                            if not isinstance(node, dict):
                                continue
                            message = node.get("message")
                            if message is None:
                                counts["nodes_without_message"] += 1
                                continue
                            counts["messages"] += 1
                            if counts["messages"] > limits.max_total_messages:
                                raise ServiceError("preview_message_limit", "总消息数超过显式预览限额")
                            if not isinstance(message, dict):
                                anomalies["invalid_message"] += 1
                                continue
                            author = message.get("author")
                            role = author.get("role") if isinstance(author, dict) else message.get("role")
                            role = role if role in ("user", "assistant", "tool", "system") else "third_party"
                            roles[role] += 1
                            branches["unknown" if main is None else "main" if node_id in main else "other"] += 1
                            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
                            counts["hidden_messages"] += metadata.get("is_visually_hidden_from_conversation") is True or message.get("is_hidden") is True
                            kind, unsupported, empty = _content(message)
                            content_types[kind] += 1
                            counts["unsupported_messages"] += unsupported
                            counts["empty_text_messages"] += empty
                            if unsupported:
                                unsupported_types[kind] += 1
                            try:
                                stamp = timestamp(message.get("create_time"))
                                if stamp:
                                    earliest, latest = min(earliest or stamp, stamp), max(latest or stamp, stamp)
                                else:
                                    anomalies["missing_message_time"] += 1
                            except ServiceError:
                                anomalies["invalid_message_time"] += 1
                            try:
                                normalized = normalize_message(message, cid, pos, parent=node.get("parent"), node_id=node_id)
                                key, _ = Importer.key(normalized, scope)
                                counts["duplicate_versions_in_archive"] += key in keys
                                keys.add(key)
                                counts["existing_exact_message_versions"] += key in existing_keys
                                counts["existing_changed_message_ids"] += key not in existing_keys and (cid, normalized["message_id"]) in existing_ids
                            except (ServiceError, ValueError, TypeError, AttributeError):
                                anomalies["legacy_fingerprint_unavailable"] += 1
            chat_bytes = sum(i["expanded_bytes"] for i in selected)
    except ServiceError:
        raise
    except (ijson.JSONError, zipfile.BadZipFile, ValueError, TypeError, KeyError, OSError, EOFError):
        raise ServiceError("preview_parse", "对话卷解析失败；未写入原始会话") from None
    if file_sha256(path) != sha256:
        raise ServiceError("preview_source_changed", "ZIP 在预览期间发生变化；拒绝保存确认凭据")
    return {
        "format": "zero-memory-openai-preview/v0.3a", "preview_id": str(uuid4()), "created_at": now(),
        "status": "awaiting_confirmation", "source_path": str(path), "zip_sha256": sha256,
        "zip_bytes": path.stat().st_size, "limits": asdict(limits), "scope": scope.model_dump(),
        "members": members, "conversation_volumes": len(selected), "conversations": len(unique_ids),
        "counts": dict(counts), "roles": dict(roles), "content_types": dict(content_types),
        "branches": dict(branches), "unsupported_types": dict(unsupported_types), "anomalies": dict(anomalies),
        "system_messages": roles["system"], "earliest_message_utc": earliest, "latest_message_utc": latest,
        "existing_conversations": len(unique_ids & existing_conversations),
        "new_conversations": len(unique_ids - existing_conversations),
        "unique_message_versions": len(keys), "message_key_set_sha256": digest(dumps(sorted(keys))),
        "new_legacy_message_versions": len(keys - existing_keys),
        "space": {"conversation_json_bytes": chat_bytes, "database_estimate_bytes": chat_bytes * 2,
                  "separate_fts_estimate_bytes": chat_bytes, "database_fts_wal_reserve_bytes": chat_bytes * 8},
        "policy": {"conversation_store_writes": 0, "memory_writes": 0, "model_calls": 0,
                   "network_calls": 0, "non_conversation_members_opened": 0},
        "confirmation": "确认导入 <preview_id>",
    }


def save_preview(preview, home):
    """Persist only metadata, counts and hashes; never raw message text or titles."""
    target = Path(home) / "review_queue" / "v03a" / (preview["preview_id"] + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(target, preview)
    return target


def main():
    parser = argparse.ArgumentParser(description="只读统计 OpenAI ZIP，不执行导入或模型提炼")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--max-archive-mib", type=int, default=512)
    parser.add_argument("--max-expanded-mib", type=int, default=1024)
    parser.add_argument("--max-member-mib", type=int, default=128)
    parser.add_argument("--max-conversation-mib", type=int, default=32)
    parser.add_argument("--max-members", type=int, default=1000)
    parser.add_argument("--max-ratio", type=int, default=100)
    args = parser.parse_args()
    try:
        limits = PreviewLimits(max_archive_bytes=args.max_archive_mib * 1024**2,
                               max_expanded_bytes=args.max_expanded_mib * 1024**2,
                               max_member_bytes=args.max_member_mib * 1024**2,
                               max_conversation_bytes=args.max_conversation_mib * 1024**2,
                               max_members=args.max_members, max_ratio=args.max_ratio)
        preview = preview_archive(args.input, args.home / "memory/zero.sqlite3", limits=limits)
        save_preview(preview, args.home)
        print(dumps({k: preview[k] for k in ("preview_id", "zip_sha256", "conversations", "counts", "roles", "branches", "anomalies")}))
        return 0
    except (ServiceError, sqlite3.Error, ValueError, OSError) as exc:
        print(dumps({"error": getattr(exc, "code", "preview_failed"), "conversation_store_writes": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
