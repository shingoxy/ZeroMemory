"""按类别或所选的批量审核：先预览数量与抽样证据，确认串绑定候选集合哈希后才执行。"""

import json
from collections import Counter
from typing import Literal

from pydantic import Field, model_validator

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import digest, dumps
from zero_memory.review.links import open_conflict_ids
from zero_memory.review.queue import defer, deferred_ids, pending_clauses, priority
from zero_memory.schemas.models import MemoryType, Scope

APPROVE_LIMIT = 50
BULK_LIMIT = 200
SELECTION_LIMIT = 30
SAMPLE_SIZE = 5
LABELS = {"reject": "驳回", "defer": "稍后处理", "approve": "批准"}


class BatchRequest(Scope):
    action: Literal["reject", "defer", "approve"]
    memory_type: MemoryType | None = None
    memory_ids: list[str] = Field(default_factory=list, max_length=SELECTION_LIMIT)
    source_type: str | None = Field(default=None, max_length=100)
    run_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    threshold: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def one_selection(self):
        # 必须二选一：按类别，或所选记忆；没有「全部批准」
        if bool(self.memory_type) == bool(self.memory_ids):
            raise ValueError("需要指定类别或所选记忆之一")
        return self


class BatchApply(BatchRequest):
    confirm: str = Field(min_length=1, max_length=200)


def _matched(db, scope, request):
    clauses, params = pending_clauses(scope, memory_type=request.memory_type, source_type=request.source_type,
                                      run_id=request.run_id)
    if request.memory_ids:
        clauses.append(f"m.id IN ({','.join('?' * len(request.memory_ids))})")
        params.extend(request.memory_ids)
    rows = db.execute(f"SELECT m.body FROM memories m WHERE {' AND '.join(clauses)}", params).fetchall()
    memories = [json.loads(row[0]) for row in rows]
    return sorted(memories, key=lambda m: (priority(m), m["extracted_at"], m["memory_id"]), reverse=True)


def _skip_reason(memory, request, deferred, conflicts):
    if memory.get("sensitivity") == "sensitive":
        return "sensitive"
    if memory["memory_id"] in deferred:
        return "deferred"
    if request.action != "approve":
        return None
    if priority(memory) < request.threshold:
        return "low_priority"
    if memory["memory_id"] in conflicts:
        return "conflict_suggestion"
    if memory.get("conflicts") or memory.get("summary_links"):
        return "needs_supersede"
    if not memory.get("evidence_verified"):
        return "unverified"
    if memory.get("source_role") != "user":
        return "not_user"
    return None


def _plan(db, scope, request):
    matched = _matched(db, scope, request)
    deferred = deferred_ids(db, scope)
    conflicts = open_conflict_ids(db, scope) if request.action == "approve" else set()
    reasons = [(memory, _skip_reason(memory, request, deferred, conflicts)) for memory in matched]
    limit = APPROVE_LIMIT if request.action == "approve" else BULK_LIMIT
    eligible = [memory for memory, reason in reasons if reason is None][:limit]
    skipped = dict(Counter(reason for _, reason in reasons if reason))
    return matched, eligible, skipped, limit


def _confirm(request, eligible):
    ids = sorted(memory["memory_id"] for memory in eligible)
    return f"确认批量{LABELS[request.action]} {request.memory_type or '所选'} {digest(dumps(ids))[:12]}"


def preview_batch(store, backend, scope, request):
    with store.read() as db:
        matched, eligible, skipped, limit = _plan(db, scope, request)
    return {
        "action": request.action, "selection": "category" if request.memory_type else "ids",
        "matched": len(matched), "eligible": len(eligible), "skipped": skipped,
        "samples": [{"memory_id": m["memory_id"], "content": m["content"], "evidence": m.get("evidence") or ""}
                    for m in eligible[:SAMPLE_SIZE]],
        "limit": limit, "confirm": _confirm(request, eligible),
        "low_priority_included": sum(1 for m in eligible if priority(m) < request.threshold),
    }


def apply_batch(store, backend, scope, request, confirm, actor="admin_batch"):
    with store.read() as db:
        _, eligible, _, _ = _plan(db, scope, request)
    if confirm != _confirm(request, eligible):
        raise ServiceError("batch_confirm", "候选集合已变化或确认串不匹配，请重新预览", 409)
    ids = [memory["memory_id"] for memory in eligible]
    if request.action == "defer":
        return {"applied": defer(store, scope, ids, actor)["updated"], "errors": []}
    applied, errors = 0, []
    for memory_id in ids:
        try:
            if request.action == "approve":
                backend.approve(memory_id, scope, actor=actor)
            else:
                backend.transition(memory_id, scope, "rejected", actor=actor, expected_status="pending_review")
            applied += 1
        except ServiceError as exc:
            # 只记录编号和错误码，不带正文
            errors.append(f"{memory_id}: {exc.code}")
    return {"applied": applied, "errors": errors}
