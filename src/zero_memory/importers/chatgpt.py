import io
import json
import math
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import ijson

from zero_memory.core.config import Settings
from zero_memory.core.errors import ServiceError
from zero_memory.core.store import digest, dumps


def safe_zip(path: Path, settings: Settings, max_files=1000, max_ratio=100):
    try:
        archive = zipfile.ZipFile(path)
        total = 0
        names = set()
        if len(archive.infolist()) > max_files:
            raise ServiceError("zip_limit", "ZIP 文件数量超过限制")
        for item in archive.infolist():
            name = item.filename.replace("\\", "/")
            parts = PurePosixPath(name)
            if (
                parts.is_absolute()
                or ".." in parts.parts
                or ":" in name
                or "\x00" in name
                or ((item.external_attr >> 16) & 0o170000) == 0o120000
            ):
                raise ServiceError("zip_path", "ZIP 包含危险路径或符号链接")
            if name in names:
                raise ServiceError("zip_duplicate", "ZIP 包含重复文件名")
            names.add(name)
            total += item.file_size
            if (
                item.file_size > settings.max_member_bytes
                or total > settings.max_expanded_bytes
                or item.file_size > max(1024 * 1024, item.compress_size * max_ratio)
            ):
                raise ServiceError("zip_limit", "ZIP 展开大小或压缩比超过限制")
            if item.flag_bits & 1:
                raise ServiceError("zip_encrypted", "不支持加密 ZIP")
        return archive
    except BaseException:
        if "archive" in locals():
            archive.close()
        raise


def timestamp(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                raise ValueError()
            return datetime.fromtimestamp(value, UTC).isoformat()
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or UTC).astimezone(UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        raise ServiceError("message_time", "消息时间无效") from None


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_content(x) for x in value if x is not None)
    if isinstance(value, dict):
        if "parts" in value:
            return text_content(value["parts"])
        if isinstance(value.get("text"), str):
            return value["text"]
        # Preserve tool/non-text descriptors as inert data; never fetch attachments.
        return "[非文本内容] " + dumps(value)
    if value is None:
        return ""
    return str(value)


def normalize_message(
    raw, cid, index, *, parent=None, node_id=None, source_type="generic_import", metadata=None
):
    if not isinstance(raw, dict):
        raise ServiceError("message_schema", "消息必须是 JSON 对象")
    role = raw.get("role") or raw.get("author", {}).get("role", "third_party")
    if role not in {"user", "assistant", "system", "tool"}:
        role = "third_party"
    content = text_content(raw.get("content"))
    if len(content) > 1_000_000 or len(dumps(raw)) > 2_000_000:
        raise ServiceError("message_limit", "单条消息过大")
    stamp = timestamp(raw.get("timestamp", raw.get("created_at", raw.get("create_time"))))
    mid = str(
        raw.get("message_id") or raw.get("id") or node_id or digest(dumps([cid, index, role, content, stamp]))
    )
    if len(cid) > 300 or len(mid) > 300:
        raise ServiceError("message_id", "会话或消息 ID 过长")
    return {
        "kind": "message",
        "conversation_id": cid,
        "message_id": mid,
        "role": role,
        "content": content,
        "timestamp": stamp,
        "update_time": timestamp(raw.get("update_time")),
        "parent_id": parent if parent is not None else raw.get("parent_id"),
        "node_id": node_id or mid,
        "source_type": source_type,
        "metadata": metadata or {},
        "raw": raw,
    }


def normalize_item(item, index=0, default_cid="generic"):
    if not isinstance(item, dict):
        raise ServiceError("import_schema", "JSON 记录必须是对象")
    if item.get("record_type") == "conversation_message":
        if (
            item.get("source_type") != "chatgpt_export"
            or item.get("role") not in {"user", "assistant"}
            or not item.get("conversation_id")
            or not item.get("message_id")
            or not isinstance(item.get("content"), str)
        ):
            raise ServiceError("message_schema", "conversation_message 必须保留真实角色、会话和消息 ID")
        cid = str(item["conversation_id"])
        yield {
            "kind": "conversation", "conversation_id": cid,
            "title": str(item.get("conversation_title") or cid), "source_type": "chatgpt_export",
        }
        record = normalize_message(
            item, cid, index, source_type="chatgpt_export",
            metadata={k: item.get(k) for k in ("branch_policy", "evidence_type", "dedup_key")},
        )
        record["metadata"]["can_be_user_fact"] = item["role"] == "user"
        yield record
    elif isinstance(item.get("mapping"), dict):
        cid = str(item.get("conversation_id") or item.get("id") or digest(dumps(item)))
        graph = item["mapping"]
        # Reject cycles without inventing a branch order. All nodes remain separate.
        done = set()
        for node_id in graph:
            walk, cursor = set(), node_id
            while cursor in graph and cursor not in done:
                if cursor in walk:
                    raise ServiceError("mapping_cycle", "ChatGPT 分支父节点存在循环")
                walk.add(cursor)
                node = graph[cursor]
                if not isinstance(node, dict):
                    raise ServiceError("mapping_schema", "ChatGPT mapping 节点无效")
                cursor = node.get("parent")
            done.update(walk)
        yield {
            "kind": "conversation",
            "conversation_id": cid,
            "title": str(item.get("title", "未命名")),
            "current_node": item.get("current_node"),
            "create_time": timestamp(item.get("create_time")),
            "update_time": timestamp(item.get("update_time")),
            "source_type": "chatgpt_export",
        }
        for pos, (node_id, node) in enumerate(graph.items()):
            if node.get("message") is not None:
                yield normalize_message(
                    node["message"],
                    cid,
                    pos,
                    parent=node.get("parent"),
                    node_id=node_id,
                    source_type="chatgpt_export",
                    metadata={
                        "children": node.get("children", []),
                        "parent_missing": bool(node.get("parent") and node["parent"] not in graph),
                    },
                )
    elif isinstance(item.get("messages"), list):
        cid = str(item.get("conversation_id") or item.get("id") or digest(dumps(item)))
        yield {
            "kind": "conversation",
            "conversation_id": cid,
            "title": str(item.get("title", "未命名")),
            "source_type": "generic_import",
        }
        for pos, message in enumerate(item["messages"]):
            yield normalize_message(message, cid, pos)
    elif "role" in item or "author" in item:
        cid = str(item.get("conversation_id") or default_cid)
        yield normalize_message(item, cid, index)
    else:
        raise ServiceError("import_schema", "不支持的会话结构：需要 mapping、messages 或 role/content")


def json_records(stream, name):
    if name.lower().endswith(".jsonl"):
        for index, line in enumerate(stream):
            if len(line) > 2_000_000:
                raise ServiceError("message_limit", "JSONL 单行过大")
            if line.strip():
                yield from normalize_item(json.loads(line), index)
        return
    buffered = stream if hasattr(stream, "peek") else io.BufferedReader(stream)
    first = buffered.peek(4096).lstrip()
    if first.startswith(b"\xef\xbb\xbf"):
        buffered.read(3)
        first = buffered.peek(4096).lstrip()
    if first.startswith(b"["):
        for index, item in enumerate(ijson.items(buffered, "item", use_float=True)):
            yield from normalize_item(item, index)
    elif first.startswith(b"{"):
        # Wrapper exports {conversations:[...]} stream by conversation, including split files.
        # Identify only a top-level key, then rewind by reopening through the seekable input.
        position = buffered.tell()
        wrapper = False
        for prefix, event, value in ijson.parse(buffered, use_float=True):
            if prefix == "" and event == "map_key":
                if value == "conversations":
                    wrapper = True
                    break
                if value in {"mapping", "messages", "role", "author"}:
                    break
        buffered.seek(position)
        if wrapper:
            for index, item in enumerate(ijson.items(buffered, "conversations.item", use_float=True)):
                yield from normalize_item(item, index)
        else:
            yield from normalize_item(json.load(buffered))
    else:
        raise ServiceError("json_parse", "文件不是有效 JSON/JSONL")


def records(path: Path, settings: Settings):
    try:
        if path.suffix.lower() == ".zip":
            with safe_zip(path, settings) as archive:
                # Official exports also contain conversation asset indexes and sharing metadata.
                # Prefer their actual conversation volumes over similarly named metadata files.
                volumes = sorted(
                    x.filename
                    for x in archive.infolist()
                    if not x.is_dir()
                    and re.fullmatch(
                        r"conversations?(?:[-_]\d+)?\.jsonl?", Path(x.filename).name, re.IGNORECASE
                    )
                )
                candidates = sorted(
                    x.filename
                    for x in archive.infolist()
                    if not x.is_dir()
                    and Path(x.filename).suffix.lower() in {".json", ".jsonl"}
                    and (
                        "conversation" in Path(x.filename).name.lower()
                        or Path(x.filename).suffix.lower() == ".jsonl"
                    )
                )
                candidates = volumes or candidates
                if not candidates:
                    candidates = sorted(
                        x.filename
                        for x in archive.infolist()
                        if not x.is_dir() and Path(x.filename).suffix.lower() == ".json"
                    )
                if not candidates:
                    raise ServiceError("zip_empty", "ZIP 内没有可解析的 JSON/JSONL 对话")
                for name in candidates:
                    with archive.open(name) as stream:
                        yield from json_records(stream, name)
        else:
            with path.open("rb") as stream:
                yield from json_records(stream, path.name)
    except ServiceError:
        raise
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
        RecursionError,
        ijson.JSONError,
        zipfile.BadZipFile,
        EOFError,
        OSError,
    ):
        raise ServiceError(
            "json_parse", "解析失败：文件格式、ZIP 校验或消息结构无效；原文件不会被执行", 422
        ) from None
