"""敏感隔离队列：只逐条处理、不支持批量；驳回时清除候选正文，只保留哈希与决定记录。"""

import json
import re

from pydantic import ValidationError

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import atomic_json, digest, dumps, now
from zero_memory.extraction.cloud_batch import ALLOWED_TYPES
from zero_memory.extraction.cloud_preview import SENSITIVE
from zero_memory.schemas.models import Proposal

QUEUE_DIR = "review_queue/v03b/sensitive"
ITEM_ID = re.compile(r"^[0-9a-f]{64}$")
OUTCOMES = {"keep_sensitive": "kept_sensitive", "to_private": "moved_to_review"}


def _load(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        raise ValueError("敏感条目格式无效")
    # 待处理条目的字段类型也必须正确，否则按损坏文件跳过，不能让整个审核页报错
    if value["status"] == "pending_review" and (
            not isinstance(value.get("candidate"), dict) or not isinstance(value.get("source_message_key"), str)
            or not isinstance(value.get("run_id"), str | None)):
        raise ValueError("敏感条目字段类型无效")
    return value


def _in_scope(db, scope, key):
    return bool(key) and db.execute("SELECT 1 FROM messages WHERE key=? AND user_id=? AND agent_id=? AND project_id=?",
                                    (key, *scope.sql())).fetchone() is not None


def _rule_hits(candidate):
    text = f"{candidate.get('content') or ''}\n{candidate.get('evidence') or ''}"
    return sorted({match.group(0) for match in SENSITIVE.finditer(text)})


def _path(store, item_id):
    if not ITEM_ID.match(item_id):
        raise ServiceError("validation", "敏感条目编号无效", 422)
    return store.home / QUEUE_DIR / f"{item_id}.json"


def sensitive_items(store, scope):
    folder = store.home / QUEUE_DIR
    items, skipped = [], 0
    with store.read() as db:
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                item = _load(path) if ITEM_ID.match(path.stem) else None
            except (OSError, ValueError):
                item = None
            if item is None:
                skipped += 1
                continue
            if item["status"] != "pending_review" or not _in_scope(db, scope, item.get("source_message_key")):
                continue
            candidate = item.get("candidate") or {}
            items.append({"item_id": path.stem, "status": item["status"], "deferred": bool(item.get("deferred")),
                          "run_id": item.get("run_id"), "observed_at": item.get("observed_at"),
                          "source_message_key": item.get("source_message_key"),
                          "source_conversation_id": item.get("source_conversation_id"),
                          "candidate": candidate, "rule_hits": _rule_hits(candidate)})
    return {"items": items, "skipped_files": skipped}


def _stripped(item, status, actor):
    # 驳回后只留来源定位、哈希与决定，正文和证据不再保存在隔离文件里
    kept = {key: value for key, value in item.items() if key != "candidate"}
    return {**kept, "status": status, "candidate_sha256": digest(dumps(item.get("candidate") or {})),
            "decided_at": now(), "decided_by": actor}


def _proposal(item, sensitivity):
    candidate = item.get("candidate") or {}
    event_date = item.get("event_date")
    try:
        return Proposal(
            content=candidate.get("content") or "", evidence=candidate.get("evidence") or "",
            memory_type=candidate.get("memory_type") if candidate.get("memory_type") in ALLOWED_TYPES else "user_profile",
            confidence=float(candidate.get("confidence") or 0.6), sensitivity=sensitivity,
            fact_key=candidate.get("fact_key"), claim_kind=candidate.get("claim_kind") or "user_reported",
            source_type="chatgpt_export", source_conversation_id=item.get("source_conversation_id"),
            source_message_ids=[item.get("source_message_id")], source_message_keys=[item.get("source_message_key")],
            origin_quality="verbatim_export_text", inference_type="system_summary",
            event_time_original=event_date, event_time_precision="day" if event_date else None,
            review_note=f"DeepSeek 批次 {item.get('run_id')} / 敏感队列转入")
    except (ValidationError, TypeError, ValueError):
        raise ServiceError("sensitive_item", "敏感条目字段不完整，无法转为候选", 422) from None


def act(store, backend, scope, item_id, action, actor="admin"):
    path = _path(store, item_id)
    with store.lock():
        try:
            item = _load(path)
        except (OSError, ValueError):
            raise ServiceError("not_found", "敏感条目不存在或无法读取", 404) from None
        with store.read() as db:
            in_scope = _in_scope(db, scope, item.get("source_message_key"))
        if item["status"] != "pending_review" or not in_scope:
            raise ServiceError("not_found", "敏感条目不存在、已处理或不在授权作用域内", 404)
        if action == "defer":
            atomic_json(path, {**item, "deferred": True, "deferred_at": now(), "decided_by": actor})
            return {"item_id": item_id, "status": item["status"], "memory_id": None, "approved": False}
        if action == "reject":
            atomic_json(path, _stripped(item, "rejected", actor))
            return {"item_id": item_id, "status": "rejected", "memory_id": None, "approved": False}
        memory = backend.propose(_proposal(item, "sensitive" if action == "keep_sensitive" else "private"), scope, actor)
        approved = False
        if action == "keep_sensitive":
            try:
                backend.approve(memory.memory_id, scope, actor=actor)
                approved = True
            except ServiceError as exc:
                # 需要先处理冲突或关联摘要时保持待审核，仍在敏感视图中显示
                if exc.status != 409:
                    raise
        status = OUTCOMES[action]
        atomic_json(path, {**item, "status": status, "memory_id": memory.memory_id, "decided_at": now(),
                           "decided_by": actor})
        return {"item_id": item_id, "status": status, "memory_id": memory.memory_id, "approved": approved}


def count_run_items(store, scope, run_id):
    return sum(1 for item in sensitive_items(store, scope)["items"] if item["run_id"] == run_id)


def reject_run_items(store, scope, run_id, actor):
    rejected = 0
    with store.lock():
        for entry in sensitive_items(store, scope)["items"]:
            if entry["run_id"] != run_id:
                continue
            path = _path(store, entry["item_id"])
            atomic_json(path, _stripped(_load(path), "rejected", actor))
            rejected += 1
    return rejected
