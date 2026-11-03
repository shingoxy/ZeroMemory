"""按批次撤销：先预演计数，确认串匹配后只驳回该批次仍待审核的候选。"""

from zero_memory.core.errors import ServiceError
from zero_memory.review.runs import run_like

RUN_SQL = "user_id=? AND agent_id=? AND project_id=? AND json_extract(body,'$.review_note') LIKE ?"


def _sensitive_count(store, scope, run_id):
    # Task 7 接入敏感队列前不计数
    return 0


def _sensitive_reject(store, scope, run_id, actor):
    return 0


def revoke_run(store, backend, scope, run_id, confirm=None, actor="admin"):
    params = (*scope.sql(), run_like(run_id))
    with store.read() as db:
        statuses = dict(db.execute(f"SELECT status, count(*) FROM memories WHERE {RUN_SQL} GROUP BY 1", params).fetchall())
        pending_ids = [row[0] for row in db.execute(
            f"SELECT id FROM memories WHERE {RUN_SQL} AND status='pending_review' ORDER BY id", params)]
    pending = statuses.get("pending_review", 0)
    approved = statuses.get("approved", 0) + statuses.get("superseded", 0)
    sensitive_items = _sensitive_count(store, scope, run_id)
    if not sum(statuses.values()) and not sensitive_items:
        raise ServiceError("run_not_found", "批次不存在或不在授权作用域内", 404)
    expected = f"确认撤销批次 {run_id}"
    if confirm is None:
        return {"run_id": run_id, "pending": pending, "approved": approved,
                "other": sum(statuses.values()) - pending - approved, "sensitive_items": sensitive_items,
                "confirm": expected}
    if confirm != expected:
        raise ServiceError("revoke_confirm", "确认串与批次不匹配", 409)
    for memory_id in pending_ids:
        backend.transition(memory_id, scope, "rejected", actor=actor, audit_action="memory_run_revoke",
                           details={"run_id": run_id})
    return {"run_id": run_id, "rejected": len(pending_ids),
            "sensitive_rejected": _sensitive_reject(store, scope, run_id, actor)}
