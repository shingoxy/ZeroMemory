import json
import re
from difflib import SequenceMatcher
from uuid import uuid4

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import Store, digest, dumps, now
from zero_memory.schemas.models import Memory, Proposal, Scope, Search


class SQLiteBackend:
    def __init__(self, store: Store):
        self.store = store

    def _get(self, db, memory_id: str, scope: Scope) -> Memory:
        row = db.execute(
            "SELECT body FROM memories WHERE id=? AND user_id=? AND agent_id=? AND project_id=?",
            (memory_id, *scope.sql()),
        ).fetchone()
        if not row:
            raise ServiceError("not_found", "记忆不存在或不在授权作用域内", 404)
        return Memory.model_validate_json(row["body"])

    def get(self, memory_id: str, scope: Scope):
        with self.store.read() as db:
            return self._get(db, memory_id, scope)

    def _put(self, db, memory: Memory):
        db.execute(
            "INSERT OR REPLACE INTO memories VALUES(?,?,?,?,?,?,?)",
            (
                memory.memory_id,
                *Scope(**{k: getattr(memory, k) for k in Scope.model_fields}).sql(),
                memory.status,
                memory.content,
                memory.model_dump_json(),
            ),
        )
        db.execute(
            "INSERT OR REPLACE INTO revisions VALUES(?,?,?)",
            (memory.memory_id, memory.version, memory.model_dump_json()),
        )
        db.execute("DELETE FROM memory_fts WHERE id=?", (memory.memory_id,))
        if memory.status == "approved":
            db.execute("INSERT INTO memory_fts(id,content) VALUES(?,?)", (memory.memory_id, memory.content))

    def _verify(self, db, proposal: Proposal, scope: Scope) -> bool:
        if proposal.source_type == "agent_activity":
            from zero_memory.activity.evidence import verify_activity_proposal
            return verify_activity_proposal(db, self.store, proposal, scope)
        if proposal.activity_goal_id or proposal.activity_episode_ids or proposal.activity_artifact_ids:
            raise ServiceError("activity_provenance", "活动引用须通过真实回执校验")
        if proposal.source_type == "chatgpt_conversation_summary":
            artifact = proposal.source_artifact
            if (
                artifact is None
                or proposal.origin_quality != "summary_only"
                or proposal.source_message_ids
                or proposal.source_message_keys
                or proposal.inference_type not in {"system_summary", "assistant_inference"}
            ):
                raise ServiceError("provenance", "摘要必须保留来源文件，不能冒充逐字用户证据")
            path = self.store.home / "sources" / artifact.source_file
            if not path.exists() or digest(path.read_bytes()) != artifact.source_hash:
                raise ServiceError("provenance", "摘要来源文件缺失或校验失败")
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig").splitlines()[artifact.line - 1])
                if (
                    digest(dumps(raw)) != artifact.record_hash
                    or raw["source_role"] != proposal.source_role
                    or raw["evidence"] != proposal.evidence
                    or raw["claim_kind"] != proposal.claim_kind
                    or any(raw[k] != getattr(scope, k) for k in Scope.model_fields)
                ):
                    raise ValueError()
            except (ValueError, KeyError, IndexError):
                raise ServiceError("provenance", "摘要来源行不匹配") from None
            return False
        if proposal.source_type in {"manual", "mcp_client"}:
            if proposal.source_message_ids or proposal.source_message_keys:
                raise ServiceError("provenance", "手工和客户端记忆不能伪造导入消息引用")
            return False
        if not proposal.source_message_keys or not proposal.evidence.strip():
            raise ServiceError("provenance", "导入记忆必须有精确消息版本引用及原文证据")
        rows = []
        for key in proposal.source_message_keys:
            row = db.execute(
                "SELECT * FROM messages WHERE key=? AND user_id=? AND agent_id=? AND project_id=?",
                (key, *scope.sql()),
            ).fetchone()
            if not row or row["conversation_id"] != proposal.source_conversation_id:
                raise ServiceError("provenance", "来源消息不在此会话或授权范围内")
            if json.loads(row["body"])["source_type"] != proposal.source_type:
                raise ServiceError("provenance", "来源类型与原始导入记录不一致")
            rows.append(row)
        if set(proposal.source_message_ids) != {r["message_id"] for r in rows}:
            raise ServiceError("provenance", "来源消息 ID 与版本引用不一致")
        if any(r["role"] != proposal.source_role for r in rows):
            raise ServiceError("provenance", "来源角色与原始消息不一致")
        if not any(proposal.evidence in r["content"] for r in rows):
            raise ServiceError("provenance", "证据必须是某条来源消息的原文片段")
        if proposal.source_role != "user" and proposal.inference_type == "user_statement":
            raise ServiceError("provenance", "Assistant 或第三方内容不能被标为用户亲述")
        return True

    def summary_candidates(self, scope, content, limit=10):
        with self.store.read() as db:
            memories = [
                Memory.model_validate_json(r[0])
                for r in db.execute(
                    "SELECT body FROM memories WHERE user_id=? AND agent_id=? AND project_id=? "
                    "AND status IN ('pending_review','approved') AND json_extract(body,'$.origin_quality')='summary_only'",
                    scope.sql(),
                )
            ]
        return sorted(
            memories, key=lambda m: SequenceMatcher(None, m.content, content).ratio(), reverse=True
        )[:limit]

    def _summary_links(self, db, proposal, scope, verified, existing):
        links = list(dict.fromkeys(proposal.summary_links))
        if verified and proposal.source_role == "user":
            for old in existing:
                if old.origin_quality == "summary_only" and old.status in {"approved", "pending_review"}:
                    if (
                        (proposal.fact_key and proposal.fact_key == old.fact_key)
                        or SequenceMatcher(None, old.content, proposal.content).ratio() >= 0.55
                    ) and old.memory_id not in links:
                        links.append(old.memory_id)
        if links and (not verified or proposal.source_role != "user"):
            raise ServiceError("summary_evidence", "须先保存并核验用户原文，才能关联历史摘要")
        for memory_id in links:
            old = self._get(db, memory_id, scope)
            if old.origin_quality != "summary_only":
                raise ServiceError("summary_source", "只能关联同一作用域的摘要记忆")
        if len(links) > 10:
            raise ServiceError("summary_limit", "摘要关联超过 10 条，请分批人工整理")
        return links

    def propose(self, proposal: Proposal, scope: Scope, actor: str = "admin") -> Memory:
        if proposal.source_role != "user" and proposal.inference_type == "user_statement":
            raise ServiceError("provenance", "非用户内容不能标为用户亲述")
        with self.store.write() as db:
            verified = self._verify(db, proposal, scope)
            existing = [
                Memory.model_validate_json(r[0])
                for r in db.execute(
                    "SELECT body FROM memories WHERE user_id=? AND agent_id=? AND project_id=? AND status NOT IN ('rejected','revoked')",
                    scope.sql(),
                )
            ]
            for old in existing:
                if (
                    old.content.strip().casefold() == proposal.content.strip().casefold()
                    and old.source_message_keys == proposal.source_message_keys
                    and old.source_type == proposal.source_type
                    and old.memory_type == proposal.memory_type
                ):
                    return old
            memory = Memory(
                **proposal.model_dump(),
                **scope.model_dump(),
                memory_id=str(uuid4()),
                extracted_at=now(),
                evidence_verified=verified,
            )
            memory.summary_links = self._summary_links(db, proposal, scope, verified, existing)
            if proposal.fact_key:
                memory.conflicts = [
                    m.memory_id
                    for m in existing
                    if m.fact_key == proposal.fact_key
                    and m.content != proposal.content
                    and m.status == "approved"
                ]
            memory.conflicts = list(
                dict.fromkeys(
                    memory.conflicts
                    + [
                        m.memory_id
                        for m in existing
                        if m.memory_id in memory.summary_links and m.content != memory.content
                    ]
                )
            )
            memory.duplicate_candidates = [
                m.memory_id
                for m in existing
                if SequenceMatcher(None, m.content[:2000], proposal.content[:2000]).ratio() >= 0.88
            ][:10]
            self._put(db, memory)
            self.store.audit(db, "memory_propose", actor, memory.memory_id)
            return memory

    def approve(
        self,
        memory_id: str,
        scope: Scope,
        *,
        supersedes: str | None = None,
        confirm_external: bool = False,
        actor="admin",
    ) -> Memory:
        with self.store.write() as db:
            memory = self._get(db, memory_id, scope)
            if memory.status == "approved":
                return memory
            if memory.status != "pending_review":
                raise ServiceError("review_state", "只有待审核记忆可以批准", 409)
            # Re-evaluate at review time: a related summary may already have been rejected.
            memory.conflicts = []
            if memory.fact_key:
                memory.conflicts = [
                    r[0]
                    for r in db.execute(
                        "SELECT id FROM memories WHERE user_id=? AND agent_id=? AND project_id=? AND status='approved' AND json_extract(body,'$.fact_key')=? AND id!=?",
                        (*scope.sql(), memory.fact_key, memory_id),
                    )
                ]
            summaries = [self._get(db, mid, scope) for mid in memory.summary_links]
            active_summaries = [m for m in summaries if m.status in {"pending_review", "approved"}]
            memory.conflicts = list(
                dict.fromkeys(
                    memory.conflicts + [m.memory_id for m in active_summaries if m.content != memory.content]
                )
            )
            if (not memory.evidence_verified or memory.source_role != "user") and not confirm_external:
                raise ServiceError(
                    "external_evidence", "请明确确认这是一条手工陈述或外部推断，而非已验证用户事实", 409
                )
            if memory.conflicts and not supersedes:
                raise ServiceError("fact_conflict", "存在事实冲突，请选择取代的记忆，或先编辑事实键", 409)
            if active_summaries and {m.memory_id for m in active_summaries} != {supersedes}:
                raise ServiceError(
                    "summary_resolution", "请明确取代关联摘要；多条摘要须先人工合并或逐条整理", 409
                )
            if supersedes:
                old = self._get(db, supersedes, scope)
                summary_replacement = (
                    old.memory_id in memory.summary_links
                    and old.origin_quality == "summary_only"
                    and old.status in {"pending_review", "approved"}
                    and memory.evidence_verified
                    and memory.source_role == "user"
                )
                if not summary_replacement and (
                    old.status != "approved" or not memory.fact_key or old.fact_key != memory.fact_key
                ):
                    raise ServiceError("supersedes", "只能取代同一事实键的有效记忆", 409)
                if (
                    not summary_replacement
                    and memory.event_time
                    and old.event_time
                    and memory.event_time < old.event_time
                ):
                    raise ServiceError(
                        "temporal_conflict", "较早事件不能取代较新事实；请调整时间或人工整理", 409
                    )
                if len(memory.conflicts) > 1:
                    raise ServiceError("fact_conflict", "存在多条冲突，请先逐条撤销旧事实", 409)
                old.status = "superseded"
                old.valid_to = memory.valid_from or memory.event_time or now()
                old.version += 1
                self._put(db, old)
                memory.supersedes = old.memory_id
            for merged_id in memory.merged_from:
                old = self._get(db, merged_id, scope)
                if old.status in {"approved", "pending_review"}:
                    old.status, old.valid_to = "superseded", now()
                    old.version += 1
                    self._put(db, old)
            memory.status = "approved"
            memory.valid_from = memory.valid_from or memory.event_time or now()
            memory.version += 1
            self._put(db, memory)
            self.store.audit(db, "memory_approve", actor, memory_id, {"supersedes": supersedes})
            return memory

    def edit(self, memory_id: str, scope: Scope, proposal: Proposal, expected_version: int, actor="admin"):
        with self.store.write() as db:
            old = self._get(db, memory_id, scope)
            if old.version != expected_version:
                raise ServiceError("version_conflict", "记忆已更新，请刷新", 409)
            verified = self._verify(db, proposal, scope)
            conflicts = (
                [
                    r[0]
                    for r in db.execute(
                        "SELECT id FROM memories WHERE user_id=? AND agent_id=? AND project_id=? AND status='approved' AND json_extract(body,'$.fact_key')=? AND id!=?",
                        (*scope.sql(), proposal.fact_key, memory_id),
                    )
                ]
                if proposal.fact_key
                else []
            )
            memory = Memory(
                **{
                    **proposal.model_dump(),
                    "dedup_key": old.dedup_key or proposal.dedup_key,
                    "source_artifact": old.source_artifact or proposal.source_artifact,
                },
                **scope.model_dump(),
                memory_id=memory_id,
                extracted_at=old.extracted_at,
                version=old.version + 1,
                evidence_verified=verified,
                conflicts=conflicts,
                merged_from=old.merged_from,
                merged_evidence=old.merged_evidence,
            )
            memory.summary_links = self._summary_links(db, proposal, scope, verified, [])
            memory.conflicts = list(dict.fromkeys(memory.conflicts + memory.summary_links))
            self._put(db, memory)
            self.store.audit(db, "memory_edit", actor, memory_id, {"version": memory.version})
            return memory

    def transition(self, memory_id, scope, status, actor="admin"):
        if status not in {"revoked", "rejected"}:
            raise ServiceError("status", "未知状态")
        with self.store.write() as db:
            memory = self._get(db, memory_id, scope)
            memory.status, memory.valid_to = status, now()
            memory.version += 1
            self._put(db, memory)
            self.store.audit(db, "memory_" + status, actor, memory_id)
            return memory

    def merge(self, scope, memory_ids, content, actor="admin"):
        ids = list(dict.fromkeys(memory_ids))
        if len(ids) < 2 or len(ids) > 10:
            raise ServiceError("merge_count", "合并需要 2 至 10 条不同记忆")
        with self.store.lock():
            sources = [self.get(memory_id, scope) for memory_id in ids]
            if any(m.status not in {"approved", "pending_review"} for m in sources):
                raise ServiceError("merge_status", "只能合并有效或待审核记忆")
            proposal = Proposal(
                content=content,
                memory_type=sources[0].memory_type,
                inference_type="system_summary",
                source_type="manual",
                evidence="人工合并，完整来源见 merged_evidence",
                confidence=min(m.confidence for m in sources),
            )
            with self.store.write() as db:
                memory = Memory(
                    **proposal.model_dump(),
                    **scope.model_dump(),
                    memory_id=str(uuid4()),
                    extracted_at=now(),
                    merged_from=ids,
                    merged_evidence=[
                        {
                            k: getattr(m, k)
                            for k in (
                                "memory_id",
                                "content",
                                "evidence",
                                "source_type",
                                "source_conversation_id",
                                "source_message_ids",
                                "source_message_keys",
                                "source_role",
                                "inference_type",
                                "event_time",
                                "confidence",
                                "evidence_verified",
                                "merged_from",
                                "merged_evidence",
                                "claim_kind",
                                "origin_quality",
                                "source_artifact",
                                "dedup_key",
                                "summary_links",
                                "activity_goal_id",
                                "activity_episode_ids",
                                "activity_artifact_ids",
                            )
                        }
                        for m in sources
                    ],
                )
                self._put(db, memory)
                self.store.audit(db, "memory_merge_propose", actor, memory.memory_id, {"merged_from": ids})
            return memory

    def undo(self, memory_id, scope, version: int, actor="admin"):
        with self.store.read() as db:
            old = self._get(db, memory_id, scope)
            row = db.execute(
                "SELECT body FROM revisions WHERE memory_id=? AND version=?", (memory_id, version)
            ).fetchone()
            if not row:
                raise ServiceError("not_found", "历史版本不存在", 404)
            body = json.loads(row[0])
            proposal = Proposal(**{k: body[k] for k in Proposal.model_fields if k in body})
        return self.edit(memory_id, scope, proposal, old.version, actor)

    def delete(self, memory_id: str, scope: Scope, actor="admin") -> None:
        with self.store.write() as db:
            self._get(db, memory_id, scope)
            db.execute("PRAGMA secure_delete=ON")
            db.execute("DELETE FROM memory_fts WHERE id=?", (memory_id,))
            db.execute("DELETE FROM revisions WHERE memory_id=?", (memory_id,))
            db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            self.store.audit(db, "memory_delete", actor, memory_id)

    def search(self, query: Search) -> list[dict]:
        clauses, params = ["m.user_id=?", "m.agent_id=?", "m.project_id=?"], list(query.sql())
        if query.exclude_sensitive:
            clauses.append("json_extract(m.body,'$.sensitivity')!='sensitive'")
        for key in ("status",):
            if getattr(query, key):
                clauses.append(f"m.{key}=?")
                params.append(getattr(query, key))
        if query.status == "approved":
            stamp = now()
            clauses += [
                "(json_extract(m.body,'$.valid_from') IS NULL OR json_extract(m.body,'$.valid_from')<=?)",
                "(json_extract(m.body,'$.valid_to') IS NULL OR json_extract(m.body,'$.valid_to')>?)",
            ]
            params += [stamp, stamp]
        for key in ("memory_type", "source_type"):
            if getattr(query, key):
                clauses.append(f"json_extract(m.body,'$.{key}')=?")
                params.append(getattr(query, key))
        for key, op in (("since", ">="), ("until", "<=")):
            if getattr(query, key):
                clauses.append(
                    f"COALESCE(json_extract(m.body,'$.event_time'),json_extract(m.body,'$.extracted_at')){op}?"
                )
                params.append(getattr(query, key))
        text = query.query.strip()
        # Trigram FTS handles Chinese substrings >= 3 characters; short queries use literal LIKE.
        fts = len(text) >= 3 and query.status == "approved" and bool(re.search(r"\w", text))
        if text:
            if fts:
                clauses.append("m.id IN (SELECT id FROM memory_fts WHERE memory_fts MATCH ?)")
                params.append('"' + text.replace('"', '""') + '"')
            else:
                clauses.append("m.content LIKE ? ESCAPE '\\'")
                params.append("%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        with self.store.read() as db:
            rows = db.execute(
                "SELECT m.body FROM memories m WHERE "
                + " AND ".join(clauses)
                + " ORDER BY json_extract(m.body,'$.extracted_at') DESC,m.id LIMIT ? OFFSET ?",
                [*params, query.limit, query.offset],
            ).fetchall()
        return [
            {
                **json.loads(r[0]),
                "score": 1.0 if text else 0.0,
                "search_method": "fts5_trigram" if fts else "literal",
            }
            for r in rows
        ]

    def rebuild(self):
        with self.store.write() as db:
            db.execute("DELETE FROM memory_fts")
            db.execute("INSERT INTO memory_fts SELECT id,content FROM memories WHERE status='approved'")
