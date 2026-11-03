import json

from fastapi import APIRouter, Depends, Query

from zero_memory.core.errors import ServiceError
from zero_memory.extraction.cloud_preview import CHANGE_SIGNAL, _shingles
from zero_memory.importers.raw_history import initialized
from zero_memory.schemas.models import Scope


def raw_history_router(store, web_admin):
    router = APIRouter(prefix="/v1/raw-history", dependencies=[Depends(web_admin)])

    @router.get("/batches")
    def batches(user_id: str = "default-user", project_id: str = "default"):
        scope = Scope(user_id=user_id, project_id=project_id)
        with store.read() as db:
            if not initialized(db):
                return {"items": []}
            rows = db.execute("SELECT body FROM raw_batches WHERE json_extract(body,'$.scope.user_id')=? "
                              "AND json_extract(body,'$.scope.agent_id')=? AND json_extract(body,'$.scope.project_id')=? "
                              "ORDER BY json_extract(body,'$.updated_at') DESC LIMIT 50", scope.sql())
            return {"items": [json.loads(row[0]) for row in rows]}

    @router.get("/batches/{batch_id}/issues")
    def issues(batch_id: str, user_id: str = "default-user", project_id: str = "default",
               offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        scope = Scope(user_id=user_id, project_id=project_id)
        with store.read() as db:
            row = db.execute("SELECT body FROM raw_batches WHERE id=?", (batch_id,)).fetchone() if initialized(db) else None
            if not row or json.loads(row[0])["scope"] != scope.model_dump():
                raise ServiceError("not_found", "批次不存在", 404)
            items = db.execute("SELECT ordinal,body FROM raw_batch_items WHERE batch_id=? "
                               "AND json_extract(body,'$.reason') IS NOT NULL ORDER BY ordinal LIMIT ? OFFSET ?",
                               (batch_id, limit, offset))
            return {"items": [{"ordinal": row[0], **json.loads(row[1])} for row in items]}

    @router.get("/conversations")
    def conversations(user_id: str = "default-user", project_id: str = "default",
                      query: str = Query("", max_length=300), offset: int = Query(0, ge=0),
                      limit: int = Query(30, ge=1, le=100)):
        scope = Scope(user_id=user_id, project_id=project_id)
        clauses = ["c.user_id=?", "c.agent_id=?", "c.project_id=?"]
        params = list(scope.sql())
        query = query.strip()
        if query:
            clauses.append("(instr(coalesce(json_extract(c.body,'$.title'),''),?)>0 OR "
                           + ("c.id IN (SELECT snapshot_id FROM raw_history_fts WHERE raw_history_fts MATCH ?))"
                              if len(query) >= 3 else "EXISTS (SELECT 1 FROM raw_messages m WHERE m.snapshot_id=c.id AND instr(json_extract(m.body,'$.content_text'),?)>0))"))
            params.extend((query, '"' + query.replace('"', '""') + '"' if len(query) >= 3 else query))
        with store.read() as db:
            if not initialized(db):
                return {"items": []}
            rows = db.execute("SELECT c.id,c.body FROM raw_conversations c WHERE " + " AND ".join(clauses)
                              + " ORDER BY json_extract(c.body,'$.updated_at') DESC,c.id LIMIT ? OFFSET ?",
                              (*params, limit, offset))
            return {"items": [{"snapshot_id": row[0], **json.loads(row[1])} for row in rows]}

    @router.get("/evidence/{message_key}")
    def evidence(message_key: str, user_id: str = "default-user", project_id: str = "default"):
        scope = Scope(user_id=user_id, project_id=project_id)
        with store.read() as db:
            target = db.execute(
                "SELECT m.snapshot_id,m.node_id,m.body FROM raw_messages m "
                "JOIN raw_conversations c ON c.id=m.snapshot_id "
                "WHERE m.message_key=? AND c.user_id=? AND c.agent_id=? AND c.project_id=? "
                "ORDER BY m.snapshot_id,m.node_id LIMIT 1", (message_key, *scope.sql())
            ).fetchone() if initialized(db) else None
            if not target:
                raise ServiceError("not_found", "原始消息不存在", 404)
            item = json.loads(target["body"])
            branch = item["branch_id"]
            show_hidden = bool(item["is_hidden"] or item["is_system"])
            position = db.execute(
                "SELECT position FROM (SELECT node_id, row_number() OVER (ORDER BY "
                "json_extract(body,'$.depth'),json_extract(body,'$.sibling_index'),node_id) - 1 AS position "
                "FROM raw_messages WHERE snapshot_id=? AND json_extract(body,'$.branch_id')=? "
                "AND (? OR (NOT json_extract(body,'$.is_hidden') AND NOT json_extract(body,'$.is_system')))) "
                "WHERE node_id=?", (target["snapshot_id"], branch, show_hidden, target["node_id"])
            ).fetchone()[0]
            return {"snapshot_id": target["snapshot_id"], "branch": branch, "show_hidden": show_hidden,
                    "position": position, "observed_at": item.get("observed_at")}

    @router.get("/associations/{memory_id}")
    def associations(memory_id: str, user_id: str = "default-user", project_id: str = "default"):
        scope = Scope(user_id=user_id, project_id=project_id)
        with store.read() as db:
            target = db.execute(
                "SELECT body FROM memories WHERE id=? AND user_id=? AND agent_id=? AND project_id=? "
                "AND json_extract(body,'$.source_type')='chatgpt_export'", (memory_id, *scope.sql())
            ).fetchone()
            if not target:
                raise ServiceError("not_found", "导出来源记忆不存在", 404)
            memory = json.loads(target[0])
            quote = memory.get("evidence") or ""
            terms = _shingles(quote)
            matches = []
            for row in db.execute(
                "SELECT body FROM memories WHERE user_id=? AND agent_id=? AND project_id=? "
                "AND json_extract(body,'$.source_type')='chatgpt_conversation_summary' "
                "AND json_extract(body,'$.sensitivity') IN ('public','private')", scope.sql()
            ):
                old = json.loads(row[0])
                overlap = len(terms & _shingles(old["content"]))
                if overlap >= 4:
                    matches.append((overlap, old["memory_id"], old))
            matches.sort(key=lambda match: (-match[0], match[1]))
            return {"has_time_change_wording": bool(CHANGE_SIGNAL.search(quote)), "total_matches": len(matches),
                    "items": [{"overlap": overlap, "memory": old} for overlap, _, old in matches[:3]]}

    @router.get("/conversations/{snapshot_id}/messages")
    def messages(snapshot_id: str, user_id: str = "default-user", project_id: str = "default",
                 branch: str = Query("main", max_length=300), show_hidden: bool = False,
                 offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)):
        scope = Scope(user_id=user_id, project_id=project_id)
        with store.read() as db:
            row = db.execute("SELECT body FROM raw_conversations WHERE id=? AND user_id=? AND agent_id=? AND project_id=?",
                             (snapshot_id, *scope.sql())).fetchone() if initialized(db) else None
            if not row:
                raise ServiceError("not_found", "会话不存在", 404)
            branches = [dict(r) for r in db.execute("SELECT json_extract(body,'$.branch_id') AS id, count(*) AS count "
                                                   "FROM raw_messages WHERE snapshot_id=? GROUP BY 1 ORDER BY 1", (snapshot_id,))]
            rows = db.execute("SELECT body FROM raw_messages WHERE snapshot_id=? AND json_extract(body,'$.branch_id')=? "
                              "AND (? OR (NOT json_extract(body,'$.is_hidden') AND NOT json_extract(body,'$.is_system'))) "
                              "ORDER BY json_extract(body,'$.depth'),json_extract(body,'$.sibling_index'),node_id LIMIT ? OFFSET ?",
                              (snapshot_id, branch, show_hidden, limit, offset))
            return {"conversation": json.loads(row[0]), "branches": branches, "items": [json.loads(r[0]) for r in rows]}

    return router
