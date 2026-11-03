"""A/C 建议：本地确定性比对候选证据与已有记忆，只写建议表，不改任何记忆。"""

import json

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import digest, dumps, now
from zero_memory.extraction.cloud_preview import CHANGE_SIGNAL, _shingles
from zero_memory.review.runs import run_of
from zero_memory.review.tables import initialized, install

METHOD = "shingle3-v1"
MIN_OVERLAP = 4
MAX_TARGETS = 3
EMPTY_LINKS = {"evidence": 0, "conflict": 0}
SCOPE_SQL = "user_id=? AND agent_id=? AND project_id=?"
DECISIONS = {"confirm": "confirmed", "dismiss": "dismissed"}


def _memories(db, scope):
    rows = db.execute(f"SELECT body FROM memories WHERE {SCOPE_SQL} AND status IN ('approved','pending_review') "
                      "AND json_extract(body,'$.sensitivity')!='sensitive'", scope.sql())
    return [json.loads(row[0]) for row in rows]


def _observed_at(db, scope, memory):
    keys = memory.get("source_message_keys") or []
    row = db.execute(f"SELECT timestamp FROM messages WHERE key=? AND {SCOPE_SQL}", (keys[0], *scope.sql())).fetchone() \
        if keys else None
    return row[0] if row else None


def _link(candidate, target, overlap, observed_at):
    evidence = candidate.get("evidence") or ""
    fact_key_match = bool(candidate.get("fact_key")) and candidate.get("fact_key") == target.get("fact_key") \
        and candidate["content"] != target["content"]
    change = bool(CHANGE_SIGNAL.search(evidence))
    kind = "conflict" if change or fact_key_match else "evidence"
    return {
        "link_id": digest(dumps([candidate["memory_id"], target["memory_id"], METHOD])),
        "source_memory_id": candidate["memory_id"], "target_memory_id": target["memory_id"], "kind": kind,
        "status": "suggested",
        "signals": {"overlap": overlap, "time_change_wording": change, "fact_key_match": fact_key_match},
        "target_status": target["status"],
        "time_version": {"new_valid_from": observed_at, "old_valid_to": observed_at} if kind == "conflict" else None,
        "method": METHOD, "created_at": now(), "decided_at": None, "decided_by": None,
    }


def _findings(db, scope, candidate, memories):
    # 目标：更早写入、不同批次、非自身；候选证据与目标内容的三字片段重合不少于 MIN_OVERLAP
    terms = _shingles(candidate.get("evidence") or "")
    run = (run_of(candidate.get("review_note")) or {}).get("run_id")
    scored = []
    for target in memories:
        target_run = (run_of(target.get("review_note")) or {}).get("run_id")
        if target["memory_id"] == candidate["memory_id"] or target["extracted_at"] >= candidate["extracted_at"] \
                or (run and run == target_run):
            continue
        overlap = len(terms & _shingles(target["content"]))
        if overlap >= MIN_OVERLAP:
            scored.append((overlap, target))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["memory_id"]))
    observed_at = _observed_at(db, scope, candidate) if scored else None
    return [_link(candidate, target, overlap, observed_at) for overlap, target in scored[:MAX_TARGETS]]


def suggest_links(store, scope, actor="admin"):
    with store.read() as db:
        memories = _memories(db, scope)
        candidates = [m for m in memories if m["status"] == "pending_review" and m["source_type"] == "chatgpt_export"]
        found = [link for c in candidates for link in _findings(db, scope, c, memories)]
    result = {"candidates": len(candidates), "created": 0, **EMPTY_LINKS}
    if not found:
        return result
    with store.write() as db:
        install(db)
        for link in found:
            result["created"] += db.execute("INSERT OR IGNORE INTO memory_links VALUES(?,?,?,?,?)",
                                            (link["link_id"], *scope.sql(), dumps(link))).rowcount
            status = db.execute("SELECT json_extract(body,'$.status') FROM memory_links WHERE id=?",
                                (link["link_id"],)).fetchone()[0]
            if status != "dismissed":
                result[link["kind"]] += 1
        if result["created"]:
            store.audit(db, "review_links_suggest", actor, METHOD, {"created": result["created"]})
    return result


def links_for(store, scope, memory_id):
    with store.read() as db:
        if not initialized(db):
            return {"items": []}
        rows = db.execute(f"SELECT body FROM memory_links WHERE {SCOPE_SQL} AND json_extract(body,'$.source_memory_id')=? "
                          "ORDER BY json_extract(body,'$.signals.overlap') DESC, id", (*scope.sql(), memory_id))
        return {"items": [json.loads(row[0]) for row in rows]}


def decide_link(store, scope, link_id, decision, actor="admin"):
    with store.write() as db:
        row = db.execute(f"SELECT body FROM memory_links WHERE id=? AND {SCOPE_SQL}", (link_id, *scope.sql())).fetchone() \
            if initialized(db) else None
        if not row:
            raise ServiceError("not_found", "关联建议不存在或不在授权作用域内", 404)
        link = {**json.loads(row[0]), "status": DECISIONS[decision], "decided_at": now(), "decided_by": actor}
        db.execute("UPDATE memory_links SET body=? WHERE id=?", (dumps(link), link_id))
        store.audit(db, "review_link_" + decision, actor, link_id)
    return link


def open_conflict_ids(db, scope) -> set[str]:
    if not initialized(db):
        return set()
    rows = db.execute(f"SELECT DISTINCT json_extract(body,'$.source_memory_id') FROM memory_links WHERE {SCOPE_SQL} "
                      "AND json_extract(body,'$.kind')='conflict' AND json_extract(body,'$.status') IN ('suggested','confirmed')",
                      scope.sql())
    return {row[0] for row in rows}


def link_counts(db, scope, memory_ids) -> dict[str, dict]:
    if not memory_ids or not initialized(db):
        return {}
    rows = db.execute(f"SELECT json_extract(body,'$.source_memory_id'), json_extract(body,'$.kind'), count(*) "
                      f"FROM memory_links WHERE {SCOPE_SQL} AND json_extract(body,'$.status')!='dismissed' "
                      f"AND json_extract(body,'$.source_memory_id') IN ({','.join('?' * len(memory_ids))}) GROUP BY 1, 2",
                      [*scope.sql(), *memory_ids])
    counts = {}
    for source, kind, total in rows:
        counts[source] = {**counts.get(source, EMPTY_LINKS), kind: total}
    return counts
