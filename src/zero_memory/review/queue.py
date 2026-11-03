"""审核队列：优先级、低优先级折叠与稍后处理标记。"""

import json

from zero_memory.core.store import dumps, now
from zero_memory.review.links import EMPTY_LINKS, link_counts
from zero_memory.review.runs import run_like, run_of
from zero_memory.review.tables import initialized, install

VIEWS = ("active", "deferred", "low_priority", "sensitive")
# 优先级 = 置信度 × 不同来源消息数（至少按 1 条计），与 priority() 保持一致
PRIORITY_SQL = ("COALESCE(json_extract(m.body,'$.confidence'),0) * "
                "max(1,(SELECT count(DISTINCT value) FROM json_each(m.body,'$.source_message_keys')))")
SENSITIVE_SQL = "json_extract(m.body,'$.sensitivity')='sensitive'"
DEFERRED_SQL = ("m.id IN (SELECT r.id FROM review_marks r WHERE r.user_id=m.user_id "
                "AND r.agent_id=m.agent_id AND r.project_id=m.project_id)")


def priority(memory: dict) -> float:
    return float(memory.get("confidence") or 0) * max(1, len(set(memory.get("source_message_keys") or [])))


def deferred_ids(db, scope) -> set[str]:
    if not initialized(db):
        return set()
    rows = db.execute("SELECT id FROM review_marks WHERE user_id=? AND agent_id=? AND project_id=?", scope.sql())
    return {row[0] for row in rows}


def pending_clauses(scope, *, memory_type=None, source_type=None, run_id=None):
    clauses = ["m.user_id=?", "m.agent_id=?", "m.project_id=?", "m.status='pending_review'"]
    params = list(scope.sql())
    for key, value in (("memory_type", memory_type), ("source_type", source_type)):
        if value:
            clauses.append(f"json_extract(m.body,'$.{key}')=?")
            params.append(value)
    if run_id:
        clauses.append("json_extract(m.body,'$.review_note') LIKE ?")
        params.append(run_like(run_id))
    return clauses, params


def view_clause(view, marked, threshold):
    deferred = DEFERRED_SQL if marked else "0"
    if view == "sensitive":
        return SENSITIVE_SQL, []
    if view == "deferred":
        return f"NOT ({SENSITIVE_SQL}) AND ({deferred})", []
    op = ">=" if view == "active" else "<"
    return f"NOT ({SENSITIVE_SQL}) AND NOT ({deferred}) AND {PRIORITY_SQL} {op} ?", [threshold]


def review_meta(memory, deferred, links, threshold):
    score = priority(memory)
    return {"priority": score, "low_priority": score < threshold, "deferred": memory["memory_id"] in deferred,
            "run": run_of(memory.get("review_note")), "links": links.get(memory["memory_id"], dict(EMPTY_LINKS))}


def queue(store, scope, *, view="active", memory_type=None, source_type=None, run_id=None,
          threshold=0.5, limit=30, offset=0):
    clauses, params = pending_clauses(scope, memory_type=memory_type, source_type=source_type, run_id=run_id)
    where = " AND ".join(clauses)
    with store.read() as db:
        marked = initialized(db)
        counts = {}
        for name in VIEWS:
            condition, extra = view_clause(name, marked, threshold)
            counts[name] = db.execute(f"SELECT count(*) FROM memories m WHERE {where} AND {condition}",
                                      [*params, *extra]).fetchone()[0]
        condition, extra = view_clause(view, marked, threshold)
        rows = db.execute(f"SELECT m.body FROM memories m WHERE {where} AND {condition} "
                          f"ORDER BY {PRIORITY_SQL} DESC, json_extract(m.body,'$.extracted_at') DESC, m.id "
                          "LIMIT ? OFFSET ?", [*params, *extra, limit, offset]).fetchall()
        memories = [json.loads(row[0]) for row in rows]
        deferred = deferred_ids(db, scope)
        links = link_counts(db, scope, [m["memory_id"] for m in memories])
    return {"items": [{**m, "review": review_meta(m, deferred, links, threshold)} for m in memories],
            "counts": counts, "offset": offset, "limit": limit, "threshold": threshold}


def defer(store, scope, memory_ids, actor, undo=False):
    # 只标记仍待审核的记忆；不改记忆状态和版本
    ids = list(dict.fromkeys(memory_ids))
    with store.write() as db:
        install(db)
        pending = {row[0] for row in db.execute(
            "SELECT id FROM memories WHERE user_id=? AND agent_id=? AND project_id=? AND status='pending_review' "
            f"AND id IN ({','.join('?' * len(ids))})", [*scope.sql(), *ids])}
        updated = 0
        for memory_id in (i for i in ids if i in pending):
            if undo:
                changed = db.execute("DELETE FROM review_marks WHERE id=? AND user_id=? AND agent_id=? AND project_id=?",
                                     (memory_id, *scope.sql())).rowcount
            else:
                changed = db.execute("INSERT OR IGNORE INTO review_marks VALUES(?,?,?,?,?)",
                                     (memory_id, *scope.sql(), dumps({"deferred_at": now(), "actor": actor}))).rowcount
            if changed:
                updated += 1
                store.audit(db, "review_undefer" if undo else "review_defer", actor, memory_id)
    return {"updated": updated, "undo": undo}
