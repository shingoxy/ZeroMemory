#!/usr/bin/env python3
"""Convert a ChatGPT data export to line-oriented, provenance-preserving messages.

No network, external dependencies, embedding model, or LLM required.
Accepts a ZIP export, a conversations.json / numbered conversations JSON, or a directory.
Handles the selected conversation branch when current_node is available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PATTERN = re.compile(r"(^|/)conversations(?:[-_]\d+)?\.json$", re.IGNORECASE)
MAX_JSON_BYTES = 2 * 1024 * 1024 * 1024  # refuses unexpectedly huge individual ZIP entries


def iso(t):
    if t is None or t == "":
        return None
    if isinstance(t, str) and not t.replace(".", "", 1).isnumeric():
        return t
    try:
        return datetime.fromtimestamp(float(t), tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError, OverflowError):
        return None


def text_from_content(content):
    """Preserve textual parts only; never fabricate image/audio transcription."""
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if isinstance(parts, list):
        lines = []
        for part in parts:
            if isinstance(part, str):
                lines.append(part)
            elif isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    lines.append(part["text"])
                elif isinstance(part.get("content"), str):
                    lines.append(part["content"])
        return "\n".join(lines)
    return content.get("text", "") if isinstance(content.get("text"), str) else ""


def selected_nodes(mapping, last):
    """Get the active branch in chronological order; fall back to all mapped nodes."""
    if last and last in mapping:
        seq, visited, cursor = [], set(), last
        while cursor and cursor not in visited and cursor in mapping:
            visited.add(cursor)
            seq.append(cursor)
            cursor = mapping[cursor].get("parent")
        seq.reverse()
        return seq, "active_branch"
    return sorted(
        mapping, key=lambda k: ((mapping[k].get("message") or {}).get("create_time") or 0, k)
    ), "all_nodes_fallback"


def normalize_conversation(c, src):
    if not isinstance(c, dict):
        return [], "invalid"
    cid = str(
        c.get("id")
        or c.get("conversation_id")
        or hashlib.sha256(json.dumps(c, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
    )
    title = c.get("title") or "Untitled"
    mapping = c.get("mapping") or {}
    if isinstance(mapping, dict) and mapping:
        nodes, branch = selected_nodes(mapping, c.get("current_node"))
        pairs = [(n, (mapping[n].get("message") or {})) for n in nodes]
    else:
        msgs = c.get("messages") or []
        branch = "flat_messages"
        pairs = [(str(m.get("id") or n), m) for n, m in enumerate(msgs) if isinstance(m, dict)]
    out = []
    for mid, m in pairs:
        role = (m.get("author") or {}).get("role") if isinstance(m.get("author"), dict) else m.get("role")
        # Tools and system messages should not become part of the user's conversation memory.
        if role not in ("user", "assistant"):
            continue
        body = text_from_content(m.get("content"))
        if not body.strip():
            continue
        message_id = str(m.get("id") or mid)
        out.append(
            {
                "record_type": "conversation_message",
                "schema_version": "0.1.0",
                "source_type": "chatgpt_export",
                "source_file": src,
                "conversation_id": cid,
                "conversation_title": title,
                "message_id": message_id,
                "role": role,
                "content": body,
                "created_at": iso(m.get("create_time")),
                "branch_policy": branch,
                "can_be_user_fact": role == "user",
                "evidence_type": "verbatim_export_text",
                "dedup_key": hashlib.sha256(f"{cid}\x1e{message_id}".encode("utf-8")).hexdigest(),
            }
        )
    return out, branch


def sources(path):
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            names = sorted(n for n in z.namelist() if PATTERN.search(n.replace("\\", "/")))
            if not names:
                raise ValueError("ZIP 内找不到 conversations.json 或 conversations-N.json")
            for name in names:
                info = z.getinfo(name)
                if info.file_size > MAX_JSON_BYTES:
                    raise ValueError(f"{name} 超出单文件安全阈值")
                with z.open(name) as f:
                    data = json.load(f)
                yield name, data
    elif path.is_dir():
        matches = sorted(p for p in path.rglob("*.json") if PATTERN.search(p.as_posix()))
        if not matches:
            raise ValueError("目录内找不到 conversations.json 或 conversations-N.json")
        for p in matches:
            yield str(p.relative_to(path)), json.loads(p.read_text(encoding="utf-8"))
    elif path.is_file() and path.suffix.lower() == ".json":
        yield path.name, json.loads(path.read_text(encoding="utf-8"))
    else:
        raise ValueError("输入须为 ChatGPT 导出的 ZIP、JSON 或目录")


def run(input_path, output_path):
    input_path = Path(input_path)
    output_path = Path(output_path)
    if input_path.resolve() == output_path.resolve():
        raise ValueError("不能覆盖源文件")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    stats = {
        "conversations": 0,
        "messages": 0,
        "duplicate_messages": 0,
        "assistant_messages": 0,
        "user_messages": 0,
        "branch_policies": {},
        "warnings": [],
    }
    with output_path.open("w", encoding="utf-8") as outf:
        for src, raw in sources(input_path):
            if isinstance(raw, dict) and isinstance(raw.get("conversations"), list):
                convs = raw["conversations"]
            elif isinstance(raw, list):
                convs = raw
            elif isinstance(raw, dict):
                convs = [raw]
            else:
                stats["warnings"].append(f"{src}: 数据结构不是 conversation 列表")
                continue
            for c in convs:
                messages, policy = normalize_conversation(c, src)
                if policy == "invalid":
                    continue
                stats["conversations"] += 1
                stats["branch_policies"][policy] = stats["branch_policies"].get(policy, 0) + 1
                for m in messages:
                    if m["dedup_key"] in seen:
                        stats["duplicate_messages"] += 1
                        continue
                    seen.add(m["dedup_key"])
                    outf.write(json.dumps(m, ensure_ascii=False) + "\n")
                    stats["messages"] += 1
                    stats[m["role"] + "_messages"] += 1
    report = output_path.with_suffix(".report.json")
    report.write_text(
        json.dumps(
            {
                "source": str(input_path),
                "output": str(output_path),
                "statistics": stats,
                "limitations": [
                    "只保留 user/assistant 文本消息；图片、音频、附件未自动转录",
                    "在存在 current_node 时仅选活跃分支；否则保留所有消息节点并标记 fallback",
                    "助手回答不等于用户事实；提炼阶段必须区分说话者",
                    "该工具仅作格式转换，不做记忆事实抽取",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return stats, report


def main(argv=None):
    p = argparse.ArgumentParser(description="离线转换 ChatGPT 导出 ZIP/JSON 为 ZeroMemory 消息 JSONL")
    p.add_argument("--input", "-i", required=True, help="ZIP、conversations.json、编号 JSON 或目录")
    p.add_argument("--output", "-o", required=True, help="输出 JSONL 路径")
    args = p.parse_args(argv)
    try:
        s, report = run(args.input, args.output)
    except (ValueError, OSError, zipfile.BadZipFile, json.JSONDecodeError) as e:
        p.error(str(e))
    print(json.dumps({"ok": True, **s, "report": str(report)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
